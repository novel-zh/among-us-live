/* Original vector panels; Pointer Events work with mouse, pen and a finger. */
window.GameTasks = (() => {
  const COLORS = ['#ef4d58','#459dff','#f5da52','#ef75ca'];
  const NAMES = ['Красный','Синий','Жёлтый','Розовый'];
  const SVG = 'http://www.w3.org/2000/svg';
  function beep(ok=true){
    try{const A=window.AudioContext||window.webkitAudioContext;const c=new A(),o=c.createOscillator(),g=c.createGain();o.type='sine';o.frequency.value=ok?740:180;g.gain.setValueAtTime(.035,c.currentTime);g.gain.exponentialRampToValueAtTime(.001,c.currentTime+.1);o.connect(g);g.connect(c.destination);o.start();o.stop(c.currentTime+.12);o.onended=()=>c.close();}catch{}
  }
  function mount(root,task,onSubmit){
    let disposed=false,busy=false,frame=0;
    const cleanups=[], timers=[];
    root.className='task-console';
    const delay=ms=>new Promise(r=>{const id=setTimeout(r,ms);timers.push(id);});
    const listen=(el,type,fn)=>{el.addEventListener(type,fn);cleanups.push(()=>el.removeEventListener(type,fn));};
    function status(text,kind=''){const e=root.querySelector('.terminal-status');if(e){e.textContent=text;e.className='terminal-status '+kind;}}
    async function send(answer){
      if(busy||disposed)return false;busy=true;
      try{
        const result=await onSubmit(answer);
        if(disposed)return false;
        beep();root.insertAdjacentHTML('beforeend','<div class="task-success" role="status"><span>✓</span><strong>'+((result?.done===false)?'ЭТАП<br>ВЫПОЛНЕН':'ЗАДАНИЕ<br>ВЫПОЛНЕНО')+'</strong></div>');
        await delay(1100);if(!disposed)root.dispatchEvent(new CustomEvent('task-complete',{bubbles:true}));return true;
      }catch(e){if(!disposed){beep(false);status(e.message,'bad');}return false;}finally{busy=false;}
    }
    const kind=task.station.kind;
    if(kind==='wires')wires();else if(kind==='card_swipe')swipe();else if(kind==='sequence')sequence();else if(['calibration','tuning'].includes(kind))calibration();else SkeldTasks.build(root,task,{listen,delay,send,status,isDisposed:()=>disposed,onCleanup:f=>cleanups.push(f)});

    function wires(){
      const order=task.puzzle.right, answer=Array(4).fill(null);let dragging=null,keyboard=null;
      root.innerHTML=`<div class="panel-caption"><span>ЭЛЕКТРОПИТАНИЕ</span><b class="wire-count">0 / 4</b></div><svg class="wire-board" viewBox="0 0 360 350" aria-label="Панель проводки"><defs><pattern id="wire-grid" width="18" height="18" patternUnits="userSpaceOnUse"><path d="M18 0H0V18" fill="none" stroke="#35444b" stroke-width=".5"/></pattern></defs><rect x="4" y="4" width="352" height="342" rx="14" fill="#26343b" stroke="#111a21" stroke-width="8"/><rect x="15" y="15" width="330" height="320" fill="url(#wire-grid)" rx="8"/><g class="cables"></g>${COLORS.map((color,i)=>`<g role="button" tabindex="0" data-left="${i}" aria-label="${NAMES[i]} провод: перетащить" class="wire-socket"><rect x="15" y="${40+i*76}" width="39" height="35" rx="7" fill="#121d25" stroke="#0a1117" stroke-width="4"/><circle cx="41" cy="${58+i*76}" r="10" fill="${color}" stroke="#111a21" stroke-width="4"/></g>`).join('')}${order.map((color,i)=>`<g role="button" tabindex="0" data-right="${color}" aria-label="${NAMES[color]} контакт" class="wire-socket"><rect x="306" y="${40+i*76}" width="39" height="35" rx="7" fill="#121d25" stroke="#0a1117" stroke-width="4"/><circle cx="319" cy="${58+i*76}" r="10" fill="${COLORS[color]}" stroke="#111a21" stroke-width="4"/></g>`).join('')}<g class="screws" fill="#86969d" stroke="#0e171e" stroke-width="3"><circle cx="18" cy="18" r="5"/><circle cx="342" cy="18" r="5"/><circle cx="18" cy="332" r="5"/><circle cx="342" cy="332" r="5"/></g></svg><div class="terminal-status">ПЕРЕТАЩИТЕ ПРОВОДА К ПАРНЫМ КОНТАКТАМ</div>`;
      const svg=root.querySelector('svg'),cables=root.querySelector('.cables');
      function point(e){const p=svg.createSVGPoint();p.x=e.clientX;p.y=e.clientY;return p.matrixTransform(svg.getScreenCTM().inverse());}
      function draw(){
        cables.replaceChildren();
        for(let i=0;i<4;i++){
          const y=58+i*76,target=answer[i],end=target!=null?{x:319,y:58+order.indexOf(target)*76}:dragging?.i===i?dragging.p:{x:85,y:y+9};
          const bend=Math.max(22,(end.x-41)*.45),d=`M41 ${y} C${41+bend} ${y},${end.x-bend} ${end.y},${end.x} ${end.y}`;
          for(const [width,color] of [[12,'#10191f'],[7,COLORS[i]],[2,'#ffffff55']]){const p=document.createElementNS(SVG,'path');p.setAttribute('d',d);p.setAttribute('fill','none');p.setAttribute('stroke',color);p.setAttribute('stroke-width',width);p.setAttribute('stroke-linecap','round');cables.append(p);}
        }
      }
      async function connect(i,color){
        if(i===color){answer[i]=color;beep();const n=answer.filter(x=>x!=null).length;root.querySelector('.wire-count').textContent=n+' / 4';status(n===4?'ЦЕПЬ ЗАМКНУТА':'КОНТАКТ УСТАНОВЛЕН','good');if(n===4)await send(answer);}
        else{beep(false);status('ЦВЕТА НЕ СОВПАДАЮТ','bad');}draw();
      }
      listen(svg,'pointerdown',e=>{const socket=e.target.closest('[data-left]');if(!socket||busy)return;const i=+socket.dataset.left;if(answer[i]!=null)return;e.preventDefault();dragging={i,p:point(e),pointer:e.pointerId};svg.setPointerCapture(e.pointerId);draw();});
      listen(svg,'pointermove',e=>{if(!dragging||e.pointerId!==dragging.pointer)return;e.preventDefault();dragging.p=point(e);draw();});
      listen(svg,'pointerup',async e=>{if(!dragging||e.pointerId!==dragging.pointer)return;const {i}=dragging,p=point(e);dragging=null;const pos=order.findIndex((_,j)=>Math.hypot(319-p.x,58+j*76-p.y)<30);if(pos>=0)await connect(i,order[pos]);draw();});
      listen(svg,'pointercancel',()=>{dragging=null;draw();});
      listen(svg,'keydown',async e=>{if(!['Enter',' '].includes(e.key)||busy)return;const left=e.target.closest('[data-left]'),right=e.target.closest('[data-right]');if(left){keyboard=+left.dataset.left;status(NAMES[keyboard].toUpperCase()+' ПРОВОД ВЫБРАН');}else if(right&&keyboard!=null){await connect(keyboard,+right.dataset.right);keyboard=null;}e.preventDefault();});draw();
    }

    function swipe(){
      root.innerHTML=`<div class="panel-caption"><span>КАРТА ДОСТУПА</span><b>ADMIN</b></div><div class="card-reader"><div class="reader-screen"><span class="reader-led"></span><div class="terminal-status">ДОСТАНЬТЕ КАРТУ ИЗ КОШЕЛЬКА</div></div><div class="reader-rail"><span class="rail-arrow">➜</span><span class="rail-ticks"></span><span class="rail-label">ПРОВЕДИТЕ ВПРАВО</span><span class="end-stop"></span></div><div class="wallet"><div class="wallet-seam"></div></div><button type="button" class="access-card" aria-label="Карта доступа: вставить и провести"><span class="card-stripe"></span><span class="card-photo">◉</span><span class="card-text">CREWMATE<br><b>ACCESS PASS</b></span><span class="card-chip">▦</span></button></div><p class="task-hint">Нажмите на карту, затем проведите её пальцем через считыватель с ровной скоростью.</p>`;
      const card=root.querySelector('.access-card'),rail=root.querySelector('.reader-rail');let inserted=false,drag=null;
      function reset(){card.style.setProperty('--card-x','0px');card.classList.remove('dragging');root.querySelector('.reader-led').classList.remove('reading');}
      listen(card,'pointerdown',e=>{
        if(busy||drag)return;e.preventDefault();
        if(!inserted){inserted=true;card.classList.add('inserted');status('ПРОВЕДИТЕ КАРТУ ВПРАВО');beep();return;}
        const travel=rail.clientWidth-card.offsetWidth-8;if(travel<=0)return;
        drag={pointer:e.pointerId,start:e.clientX,startY:e.clientY,t:performance.now(),x:0,y:0,travel,samples:[{x:0,y:0,t:0}]};
        card.setPointerCapture(e.pointerId);card.classList.add('dragging');root.querySelector('.reader-led').classList.add('reading');status('СЧИТЫВАНИЕ…');
        function sample(){if(!drag||disposed)return;const t=performance.now()-drag.t;drag.samples.push({x:drag.x,y:drag.y,t});if(drag.samples.length<295)frame=requestAnimationFrame(sample);}
        frame=requestAnimationFrame(sample);
      });
      listen(card,'pointermove',e=>{if(!drag||drag.pointer!==e.pointerId)return;e.preventDefault();drag.x=Math.min(1,Math.max(0,(e.clientX-drag.start)/drag.travel));drag.y=(e.clientY-drag.startY)/80;card.style.setProperty('--card-x',(drag.x*drag.travel)+'px');});
      listen(card,'pointerup',async e=>{
        if(!drag||drag.pointer!==e.pointerId)return;const d=drag;drag=null;cancelAnimationFrame(frame);
        const t=performance.now()-d.t;if(t>d.samples.at(-1).t)d.samples.push({x:d.x,y:d.y,t});
        if(d.x<.92){status('КАРТА ПРОВЕДЕНА НЕ ДО КОНЦА','bad');beep(false);reset();return;}
        const ok=await send({samples:d.samples});if(!ok&&!disposed){reset();root.querySelector('.reader-led').classList.add('failed');setTimeout(()=>root.querySelector('.reader-led')?.classList.remove('failed'),600);}
      });
      listen(card,'pointercancel',()=>{drag=null;cancelAnimationFrame(frame);reset();status('ПОВТОРИТЕ ПРОХОД КАРТЫ');});
    }

    function calibration(){
      const rotors=task.puzzle.rotors;let stage=0,hits=[];
      const base=task.time-task.started,started=performance.now();
      const elapsed=()=>base+(performance.now()-started)/1000;
      root.innerHTML=`<div class="panel-caption"><span>РАСПРЕДЕЛИТЕЛЬ</span><b class="cal-count">0 / 3</b></div><div class="distributor">${rotors.map((r,i)=>`<div class="cal-row ${i?'locked':'active'}"><svg viewBox="0 0 150 150" aria-label="Контакт ${i+1}"><circle cx="75" cy="75" r="55" fill="#182c35" stroke="#101a22" stroke-width="10"/><path d="M55 23 A56 56 0 0 1 95 23" fill="none" stroke="#6bdf8b" stroke-width="12"/><g class="rotor"><path d="M75 75V20" stroke="${COLORS[i]}" stroke-width="11" stroke-linecap="round"/><circle cx="75" cy="75" r="14" fill="#9eafb5" stroke="#111c23" stroke-width="6"/></g></svg><div class="cal-meter"><div class="power-cable"></div><span class="cal-light"></span><button class="cal-button" data-stage="${i}" ${i?'disabled':''}>СТОП</button></div></div>`).join('')}</div><div class="terminal-status">ОСТАНОВИТЕ КОНТАКТ В ЗЕЛЁНОМ СЕКТОРЕ</div>`;
      function animate(){if(disposed)return;const t=elapsed();root.querySelectorAll('.rotor').forEach((el,i)=>{const angle=i<stage?0:(rotors[i].phase+t*rotors[i].speed)%360;el.setAttribute('transform',`rotate(${angle} 75 75)`);});frame=requestAnimationFrame(animate);}animate();
      root.querySelectorAll('.cal-button').forEach(button=>listen(button,'click',async()=>{
        if(busy||+button.dataset.stage!==stage)return;const t=elapsed(),r=rotors[stage],angle=(r.phase+t*r.speed)%360;
        if(Math.min(angle,360-angle)>task.puzzle.tolerance){stage=0;hits=[];beep(false);status('ПРОМАХ. КАЛИБРОВКА СБРОШЕНА','bad');}
        else{hits.push(t);stage++;beep();status(stage===3?'ПИТАНИЕ СТАБИЛИЗИРОВАНО':'КОНТАКТ ЗАФИКСИРОВАН','good');}
        root.querySelector('.cal-count').textContent=stage+' / 3';
        root.querySelectorAll('.cal-row').forEach((el,i)=>{el.className='cal-row '+(i<stage?'fixed':i===stage?'active':'locked');el.querySelector('button').disabled=i!==stage;});
        if(stage===3){const ok=await send({hits});if(!ok&&!disposed){stage=0;hits=[];root.querySelectorAll('.cal-row').forEach((el,i)=>{el.className='cal-row '+(i?'locked':'active');el.querySelector('button').disabled=i!==0;});}}
      }));
    }

    function sequence(){
      let answer=[],playing=false,round=1;
      root.innerHTML=`<div class="panel-caption"><span>РЕАКТОР / ПАМЯТЬ</span><b>SEQUENCE</b></div><div class="reactor"><div class="reactor-lights">${Array.from({length:9},(_,i)=>`<span data-lamp="${i}"></span>`).join('')}</div><div class="reactor-pad">${Array.from({length:9},(_,i)=>`<button data-pad="${i}" aria-label="Кнопка реактора ${i+1}" disabled></button>`).join('')}</div></div><div class="sequence-progress">${task.puzzle.pattern.map(()=>'<i></i>').join('')}</div><div class="terminal-status">ЗАПОМНИТЕ СИГНАЛЫ И ПОВТОРИТЕ</div><button class="btn primary wide play-reactor">ПОКАЗАТЬ СИГНАЛ</button>`;
      const pads=()=>root.querySelectorAll('[data-pad]');
      listen(root.querySelector('.play-reactor'),'click',async()=>{if(playing||busy)return;playing=true;answer=[];pads().forEach(b=>b.disabled=true);root.querySelectorAll('.sequence-progress i').forEach(x=>x.classList.remove('on'));status('ЗАПОМИНАЙТЕ…');
        for(const n of task.puzzle.pattern.slice(0,round)){if(disposed)return;const l=root.querySelector(`[data-lamp="${n}"]`);l.classList.add('on');beep();await delay(330);l.classList.remove('on');await delay(180);}
        if(disposed)return;playing=false;pads().forEach(b=>b.disabled=false);status('ВАША ОЧЕРЕДЬ: '+round+' СИГНАЛ'+(round===1?'':'А'));
      });
      pads().forEach(b=>listen(b,'click',async()=>{if(playing||busy)return;const n=+b.dataset.pad;b.classList.add('lit');timers.push(setTimeout(()=>b.classList.remove('lit'),180));
        if(n!==task.puzzle.pattern[answer.length]){answer=[];round=1;beep(false);pads().forEach(b=>b.disabled=true);root.querySelectorAll('.sequence-progress i').forEach(x=>x.classList.remove('on'));status('НЕВЕРНО. НАЧНИТЕ С ПЕРВОЙ ПОСЛЕДОВАТЕЛЬНОСТИ','bad');return;}
        answer.push(n);beep();root.querySelectorAll('.sequence-progress i').forEach((x,i)=>x.classList.toggle('on',i<answer.length));if(answer.length===round){pads().forEach(b=>b.disabled=true);if(round===5)await send(answer);else{round++;answer=[];status('ВЕРНО. ПОКАЖИТЕ ПОСЛЕДОВАТЕЛЬНОСТЬ '+round+' / 5','good');}}
      }));
    }
    return {destroy(){disposed=true;cancelAnimationFrame(frame);timers.forEach(clearTimeout);cleanups.forEach(f=>f());}};
  }
  return {mount};
})();
