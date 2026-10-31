window.SabotageUI=(()=>{
  let noticeTimer;
  function sync(room,hasTask=false,hasRepair=false){
    const s=room?.phase==='running'?room.sabotage:null,body=document.body;
    body.classList.toggle('sabotage-active',!!s);
    body.classList.toggle('comms-hacked',s?.kind==='comms');
    const hud=document.querySelector('#sabotage-hud'),notice=document.querySelector('#sabotage-notice'),note=document.querySelector('.task-sab-note');
    hud.hidden=!s;
    if(!s){notice.hidden=true;notice.replaceChildren();if(note)note.textContent='';return;}
    if(note)note.textContent='САБОТАЖ: '+s.name+(s.deadline?' · '+Math.max(0,Math.ceil(s.deadline-(room.paused_at??(Date.now()/1000+window.sabotageClockOffset))))+' сек.':' · отбейте атаку у сервера');
    const key='aul_sab_'+room.code+'_'+room.me?.id+'_'+s.id;
    if(!sessionStorage.getItem(key)){
      sessionStorage.setItem(key,'1');clearTimeout(noticeTimer);
      if(!hasTask&&!hasRepair){notice.replaceChildren();const inner=document.createElement('div'),title=document.createElement('strong'),text=document.createElement('p');title.textContent=s.kind==='comms'?'СИСТЕМА ВЗЛОМАНА':'САБОТАЖ!';text.textContent=s.name+(s.deadline?' · 45 секунд на устранение':' · отбейте атаку у серверной панели');inner.append(title,text);notice.append(inner);notice.hidden=false;noticeTimer=setTimeout(()=>notice.hidden=true,2200);}
    }
    if(hasTask||hasRepair)notice.hidden=true;
  }
  function mount(root,grant,call,onDone){
    let disposed=false,finished=false,held=false,heartbeatTimer,pending=null;
    const cleanup=[],timers=[];let leaseTimer,leaseBusy=false,leaseLost=false;
    const listen=(el,type,fn)=>{el.addEventListener(type,fn);cleanup.push(()=>el.removeEventListener(type,fn));};
    const status=text=>{if(!disposed)root.querySelector('.repair-status').textContent=text;};
    function success(){if(disposed||finished)return;finished=true;clearInterval(leaseTimer);held=false;clearInterval(heartbeatTimer);root.classList.add('repair-success');status(grant.kind==='reactor'?'РЕАКТОР В НОРМЕ':grant.kind==='comms'?'АТАКА ОТБИТА. СВЯЗЬ ВОССТАНОВЛЕНА':'КИСЛОРОД ВОССТАНОВЛЕН');timers.push(setTimeout(()=>{if(!disposed)onDone();},850));}
    const clock=grant.deadline?`<div class="repair-clock" data-deadline="${grant.deadline}"></div>`:'';
    if(grant.kind==='comms'){
      const board=grant.puzzle,directions=[...board.directions],names=['вверх','вправо','вниз','влево'];let busy=false;
      const arrowPaths=['M20 33V7M9 18L20 7L31 18','M7 20H33M22 9L33 20L22 31','M20 7V33M9 22L20 33L31 22','M33 20H7M18 9L7 20L18 31'];
      const arrow=(d,end=false)=>`<svg viewBox="0 0 40 40" aria-hidden="true"><path d="${end?'M9 9H31V31H9Z':arrowPaths[d]}"/></svg>`;
      root.classList.add('comms-repair');
      root.innerHTML=`<div class="route-terminal-title">C:\\SHIP\\NET&gt; recover --route</div><p class="route-instruction">Поверните стрелки и соедините А с Б.<br>Только по горизонтали и вертикали.</p><div class="route-grid-wrap"><div class="comms-route" style="--route-size:${board.size}">${directions.map((d,i)=>`<button class="route-cell ${i===board.end?'route-end':''}" data-cell="${i}" ${i===board.end?'disabled':''}><span class="route-letter">${i===board.start?'А':i===board.end?'Б':''}</span><span class="route-arrow">${arrow(d,i===board.end)}</span></button>`).join('')}</div><svg class="route-link-overlay" viewBox="0 0 ${board.size*100} ${board.size*100}" preserveAspectRatio="none" aria-hidden="true"><polyline/></svg></div><div class="route-legend"><span>А · ИСТОЧНИК</span><span>Б · СЕРВЕР</span></div><button class="route-submit" disabled>ПРОВЕРИТЬ МАРШРУТ</button><div class="repair-status"></div>`;
      function controls(trace){
        root.querySelector('.route-submit').disabled=busy||finished||leaseLost||!trace.complete;
        root.querySelectorAll('[data-cell]').forEach(el=>el.disabled=busy||finished||leaseLost||+el.dataset.cell===board.end);
      }
      function draw(){
        const trace=SabotageRoute.trace(board,directions),reached=new Set(trace.path);
        root.querySelectorAll('[data-cell]').forEach(el=>{const i=+el.dataset.cell;el.classList.toggle('route-reached',reached.has(i));el.dataset.direction=String(directions[i]);el.setAttribute('aria-label',i===board.end?'Точка Б, сервер':`${i===board.start?'Точка А. ':''}Клетка ${i+1}, стрелка ${names[directions[i]]}`);el.querySelector('.route-arrow').innerHTML=arrow(directions[i],i===board.end);});
        root.querySelector('.route-link-overlay polyline').setAttribute('points',trace.path.map(i=>`${(i%board.size)*100+50},${Math.floor(i/board.size)*100+50}`).join(' '));
        root.classList.toggle('route-connected',trace.complete);controls(trace);
        status(trace.complete?'МАРШРУТ А → Б ГОТОВ. НАЖМИТЕ ПРОВЕРИТЬ':trace.reason==='loop'?'ПЕТЛЯ В МАРШРУТЕ. ПОВЕРНИТЕ СТРЕЛКИ':'СОЕДИНИТЕ А И Б ПО ЛИНИИ БЕЗ ДИАГОНАЛЕЙ');
        return trace;
      }
      root.querySelectorAll('[data-cell]').forEach(el=>listen(el,'click',()=>{if(disposed||finished||leaseLost||busy)return;const i=+el.dataset.cell;if(i===board.end)return;directions[i]=(directions[i]+1)%4;draw();}));
      listen(root.querySelector('.route-submit'),'click',async()=>{
        if(disposed||finished||leaseLost||busy)return;const trace=draw();if(!trace.complete)return;
        busy=true;controls(trace);
        try{const result=await call('sabotage_fix',{directions:[...directions],path:[...trace.path]});if(result.resolved)success();}
        catch(e){status(e.message);}finally{busy=false;if(!disposed)controls(trace);}
      });draw();
    }else if(grant.kind==='oxygen'){
      let digits='',busy=false;
      root.innerHTML=clock+`<p class="oxygen-goal">Достаточно одной панели — А или Б</p><div class="oxygen-note">КОД ПАНЕЛИ<br><b>${grant.puzzle.code}</b></div><div class="oxygen-screen" aria-live="polite">_ _ _ _ _</div><div class="oxygen-pad">${[1,2,3,4,5,6,7,8,9,'×',0,'✓'].map(n=>`<button data-digit="${n}" aria-label="${n==='×'?'Очистить код':n==='✓'?'Подтвердить код':'Цифра '+n}">${n}</button>`).join('')}</div><div class="repair-status">ВВЕДИТЕ КОД С ЗАПИСКИ</div>`;
      root.querySelectorAll('[data-digit]').forEach(el=>listen(el,'click',async()=>{if(busy||disposed||finished||leaseLost)return;const n=el.dataset.digit;if(n==='×')digits='';else if(n==='✓'){
        if(digits.length!==5){status('НУЖНО ПЯТЬ ЦИФР');return;}busy=true;
        try{await call('sabotage_fix',{code:digits});success();}catch(e){status(e.message);digits='';}finally{busy=false;}
      }else if(digits.length<5)digits+=n;
      if(!disposed)root.querySelector('.oxygen-screen').textContent=(digits.padEnd(5,'_')).split('').join(' ');}));
    }else{
      root.innerHTML=clock+`<button class="reactor-sensor" aria-label="Удерживать сенсор реактора"><svg viewBox="0 0 240 270" aria-hidden="true"><defs><pattern id="handgrid" width="9" height="9" patternUnits="userSpaceOnUse"><path d="M9 0H0V9" fill="none" stroke="#102f3b" stroke-width="2"/></pattern></defs><path class="hand-fill" d="M63 233L31 176Q20 148 36 140Q46 136 57 157L65 168V62Q65 42 80 43Q94 43 94 62V125V34Q94 14 108 14Q123 14 123 34V124V44Q123 23 137 25Q152 25 152 45V132V70Q152 49 166 51Q181 53 181 72V169Q181 215 165 239L159 255H78Z"/><path d="M63 233L31 176Q20 148 36 140Q46 136 57 157L65 168V62Q65 42 80 43Q94 43 94 62V125V34Q94 14 108 14Q123 14 123 34V124V44Q123 23 137 25Q152 25 152 45V132V70Q152 49 166 51Q181 53 181 72V169Q181 215 165 239L159 255H78Z" fill="url(#handgrid)"/></svg><span>ПРИЛОЖИТЕ ЛАДОНЬ</span></button><div class="reactor-pair"><i data-sensor="reactor_a"></i><i data-sensor="reactor_b"></i></div><div class="repair-status">УДЕРЖИВАЙТЕ СЕНСОР. НУЖЕН ВТОРОЙ ИГРОК</div>`;
      const button=root.querySelector('.reactor-sensor');
      async function pulse(){if(disposed||!held||pending||finished||leaseLost)return;const job=call('reactor_hold',{pressed:true});pending=job;try{const response=await job;if(response.resolved)success();else update(response.state);}catch(e){held=false;button.classList.remove('held');status(e.message);}finally{if(pending===job)pending=null;}}
      function start(){if(disposed||finished||held||leaseLost)return;held=true;button.classList.add('held');pulse();heartbeatTimer=setInterval(pulse,600);}
      function stop(){if(!held)return;held=false;button.classList.remove('held');clearInterval(heartbeatTimer);const release=()=>call('reactor_hold',{pressed:false}).catch(()=>{});if(pending)pending.catch(()=>{}).then(()=>{if(!held)release();});else release();status('СЕНСОР ОТПУЩЕН');}
      listen(button,'pointerdown',e=>{e.preventDefault();button.setPointerCapture(e.pointerId);start();});listen(button,'pointerup',stop);listen(button,'pointercancel',stop);
      listen(button,'keydown',e=>{if([' ','Enter'].includes(e.key)){e.preventDefault();start();}});listen(button,'keyup',stop);listen(window,'blur',stop);listen(document,'visibilitychange',()=>{if(document.hidden)stop();});cleanup.push(stop);
    }
    function update(state){if(disposed||finished||leaseLost||!state)return;if(grant.kind==='reactor'){root.querySelectorAll('[data-sensor]').forEach(el=>el.classList.toggle('on',!!state.held[el.dataset.sensor]));status(held?(Object.values(state.held).filter(Boolean).length===2?'ОБА СЕНСОРА АКТИВНЫ. ДЕРЖИТЕ…':'ЖДЁМ ВТОРОГО ИГРОКА'):'УДЕРЖИВАЙТЕ СЕНСОР');}}
    async function renew(){
      if(disposed||finished||leaseLost||leaseBusy||document.hidden)return;leaseBusy=true;
      try{const response=await call('repair_heartbeat',{});if(!disposed)update(response.state);}
      catch(e){if(disposed||finished)return;leaseLost=true;held=false;clearInterval(heartbeatTimer);clearInterval(leaseTimer);root.querySelectorAll('button').forEach(el=>el.disabled=true);status(e.message+' Закройте панель и отсканируйте QR снова.');}
      finally{leaseBusy=false;}
    }
    // Ownership is separate from a reactor hand: an open panel renews its lease,
    // while pressing a hand still needs its much shorter continuous heartbeat.
    leaseTimer=setInterval(renew,2000);
    listen(document,'visibilitychange',()=>{if(document.hidden)call('repair_release',{}).catch(()=>{});else renew();});
    listen(window,'pagehide',()=>call('repair_release',{}).catch(()=>{}));
    return {update,get finished(){return finished;},destroy(){cleanup.forEach(fn=>fn());disposed=true;clearInterval(heartbeatTimer);clearInterval(leaseTimer);timers.forEach(clearTimeout);call('repair_release',{}).catch(()=>{});}};
  }
  return {sync,mount};
})();
