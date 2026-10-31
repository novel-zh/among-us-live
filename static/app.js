'use strict';
const $ = s => document.querySelector(s);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const phaseNames = {lobby:'Лобби',running:'Игра идёт',gathering:'Сбор на совещание',discussion:'Обсуждение',voting:'Голосование',ended:'Завершена'};
const kindIcons = {wires:'ϟ',sequence:'▦',tuning:'≋'};
const kindColors = {wires:'',sequence:'purple',tuning:'blue'};
let kindNames = {wires:'Починить проводку',card_swipe:'Провести карту',calibration:'Калибровка распределителя',sequence:'Запустить реактор',tuning:'Калибровка'};
const roleNames = {crew:'Экипаж',impostor:'Предатель'};
const statusNames = {alive:'В игре',dead:'Призрак',ejected:'Изгнан',unknown:'Участник'};
let token = sessionStorage.getItem('aul_token') || '', info = null, me = null, room = null;
let page = 'home', tab = 'player', catalog = [], dirty = false, currentPuzzle = null, currentRepair = null, scanIntent = null;
let setupStep = 1, scannedObject = null, chatHistory=[], chatKeyboard=[];
let sabotageBusy=false;
let serverOffset = 0, pollBusy = false, toastTimeout, roomSignature = '', printItems = [];
const tg = () => window.Telegram?.WebApp;
let eventKey='',deathUntil=0,deathTimer=0,meetingAdminBypass='';
function resultKey(){return `aul_result_${room?.code}_${me?.user.id}_${room?.epoch}`;}
function meetingKey(){return `aul_meeting_${room?.code}_${me?.user.id}_${room?.meeting?.id}`;}
function hideEvent(){const el=$('#game-event');el.hidden=true;el.innerHTML='';eventKey='';$('.shell').inert=false;document.body.classList.remove('event-open');}
function showEvent(key,kind,html){
  if(eventKey===key)return;
  closeModal();eventKey=key;const el=$('#game-event');el.className='game-event '+kind;
  el.innerHTML='<div class="game-event-inner">'+html+'</div>';el.hidden=false;
  $('.shell').inert=true;
  document.body.classList.add('event-open');
  try{tg()?.HapticFeedback?.notificationOccurred(kind==='victory'?'success':'warning');}catch{}
}
function syncEvents(){
  if(!room?.me){hideEvent();return;}
  const p=room.me,deathKey=`aul_death_${room.code}_${p.id}_${p.death_id||'legacy'}`;
  if(p.status==='dead'&&!sessionStorage.getItem(deathKey)){
    sessionStorage.setItem(deathKey,'1');deathUntil=Date.now()+7000;clearTimeout(deathTimer);
    deathTimer=setTimeout(()=>{deathUntil=0;syncEvents();},7050);
  }
  if(p.status!=='dead')deathUntil=0;
  if(deathUntil>Date.now()){
    showEvent(deathKey,'death','<span class="event-eyebrow">Сигнал экипажа потерян</span><span class="event-symbol">×</span><h1>ВАС УБИЛИ</h1><p>Оставайтесь на своём месте и продолжайте выполнять задания.</p><p class="event-sub">Теперь задания открываются без QR.<br>После каждого выполненного задания — 60 секунд ожидания.<br>Не раскрывайте предателя живым игрокам.</p><div class="event-line"></div>');return;
  }
  if(room.phase==='ended'&&!sessionStorage.getItem(resultKey())){
    const stopped=room.winner==='stopped',win=room.winner===p.role;
    showEvent(resultKey(),stopped?'victory':win?'victory':'defeat',`<span class="event-eyebrow">${esc(room.name)}</span><h1>${stopped?'ИГРА ЗАВЕРШЕНА':win?'ПОБЕДА':'ПОРАЖЕНИЕ'}</h1><div class="event-crew"><i></i><i></i><i></i></div><p>${stopped?'Ведущий завершил эту партию.':room.winner==='crew'?'Экипаж выполнил свою миссию.':room.end_reason==='sabotage_oxygen'?'Кислород закончился. Экипаж не успел устранить утечку.':room.end_reason==='sabotage_reactor'?'Реактор разрушен. Экипаж не успел остановить перегрев.':'Предатели захватили корабль.'}</p>${btn('dismiss-result','К результатам','','primary wide')}`);return;
  }
  if(room.phase==='gathering'&&!(room.admin&&meetingAdminBypass===room.meeting.id&&tab==='admin')){
    const m=room.meeting,alive=p.status==='alive';
    if(alive&&!m.arrived_me||!alive&&!sessionStorage.getItem(meetingKey())){
      showEvent('gather_'+m.id,'emergency',`<span class="event-eyebrow">Все игровые действия заблокированы</span><span class="event-symbol">!</span><h1>${m.reason==='body'?'ОБНАРУЖЕНО ТЕЛО':'ЭКСТРЕННОЕ СОВЕЩАНИЕ!'}</h1><p>${alive?'Пройдите в главную комнату и отсканируйте QR для совещаний.':'Живые игроки собираются в главной комнате. Оставайтесь на своём месте и наблюдайте.'}</p><p class="event-sub">Незавершённая мини-игра сброшена. Пройденные отдельные этапы сохранены.</p>${alive?btn('scan-any','Сканировать QR совещания','','primary wide'):btn('observe-meeting','Наблюдать за сбором','','primary wide')}${alive&&me.test_controller?btn('test-objects','Тест: выбрать QR','','ghost wide'):''}${alive&&info.mode==='demo'?btn('demo-arrival','Демо: сканировать QR совещания','','ghost wide'):''}${me.test_controller?btn('return-host','Вернуться к хосту','','ghost wide'):''}${room.admin?btn('event-admin','Admin-Панель','','ghost wide'):''}`);return;
    }
  }
  hideEvent();
}

async function api(path, data, method) {
  const res = await fetch('/api/' + path, {method:method || (data === undefined?'GET':'POST'),
    headers:{'Content-Type':'application/json', ...(token?{'Authorization':'Bearer '+token}:{})},
    ...(data === undefined?{}:{body:JSON.stringify(data)})});
  const value = await res.json();
  if(!res.ok){
    if(res.status === 401){token=''; sessionStorage.removeItem('aul_token');}
    throw new Error(value.error || value.detail || 'Ошибка сервера');
  }
  return value;
}
function toast(message, error=false){const n=$('#toast');n.textContent=message;n.className='show'+(error?' error':'');clearTimeout(toastTimeout);toastTimeout=setTimeout(()=>n.className='',5500);}
function modal(html){$('#modal-content').innerHTML=html;if(!$('#modal').open)$('#modal').showModal();}
function syncSabotage(){window.sabotageClockOffset=serverOffset;SabotageUI.sync(page==='room'?room:null,!!currentPuzzle,!!currentRepair);}
function closeModal(){$('#modal').classList.remove('comms-dialog');currentRepair?.controller?.destroy();currentRepair=null;if($('#modal').open)$('#modal').close();currentPuzzle?.controller?.destroy();currentPuzzle=null;scanIntent=null;scannedObject=null;syncSabotage();}
$('#close-modal').onclick=closeModal;
$('#modal').addEventListener('cancel',()=>{$('#modal').classList.remove('comms-dialog');currentRepair?.controller?.destroy();currentRepair=null;currentPuzzle?.controller?.destroy();currentPuzzle=null;scanIntent=null;scannedObject=null;syncSabotage();});
function setToken(value){token=value;sessionStorage.setItem('aul_token',token);}
function canManage(){return me?.host || me?.rooms.some(r=>r.admin);}
function now(){return Date.now()/1000+serverOffset;}
function pill(r){return `<span class="pill ${r.phase==='ended'?'gray':''}">${r.paused_at!=null?'Пауза':esc(phaseNames[r.phase])}</span>`;}
function btn(action,label,extra='',classes=''){return `<button type="button" class="btn ${classes}" data-action="${action}" ${extra}>${label}</button>`;}
function input(name,label,value,type='number',attrs=''){return `<div class="field"><label class="label" for="f-${name}">${label}</label><input id="f-${name}" name="${name}" type="${type}" value="${esc(value)}" ${attrs}></div>`;}
function icon(kind){return `<span class="icon-box ${kindColors[kind]||''}">${kindIcons[kind]||'◇'}</span>`;}

function shell(){
  if(page!=='room')$('#main').classList.remove('player-screen');
  const links=canManage()?[['home','Комната'],['catalog','Станции'],...(me.host?[['settings','Подключение']]:[])]:[];if(info?.mode==='demo'&&me&&!me.test_controller)links.push(['chat','Демочат бота']);
  $('#nav').innerHTML=links.map(([key,label])=>`<button class="nav-btn ${page===key||page==='room'&&key==='home'?'active':''}" data-action="nav" data-page="${key}">${label}</button>`).join('');
  $('#nav').hidden=!links.length;
  $('#identity').innerHTML=me?`<span>${esc(me.user.name)}</span>${info.mode==='demo'&&!me.test_controller?btn('switch','Сменить','','small ghost'):''}`:'';
  $('#connection').textContent=info?.mode==='demo'?'Демо на этом компьютере':'Сервер подключён';
}
function loginScreen(){
  closeModal();hideEvent();page='home';room=null;me=null;syncSabotage();shell();
  $('#main').innerHTML=`<div class="login"><h1>Игра начинается здесь</h1><p class="muted">${info.mode==='demo'?'Для пробной партии выберите игрока.':'Откройте игру через своего бота в Telegram.'}</p>
    ${info.mode==='demo'&&info.local?`<div class="card demo-select">${[1,2,3,4].map(n=>btn('demo-login','Игрок '+n,`data-player="demo_${n}"`)).join('')}</div>`:''}
    ${info.mode==='telegram'&&info.bot_username?btn('open-bot','Открыть Telegram-бота','','primary wide'):''}
    ${info.local?`<details class="card"><summary>Войти как ведущий</summary><form id="owner-login" class="details-content"><p class="muted small">Откройте ссылку из окна START.cmd или вставьте её ключ.</p><label class="label" for="owner-key">Ключ владельца</label><input id="owner-key" name="key" type="password" required autocomplete="off"><button class="btn primary wide" style="margin-top:16px">Войти</button></form></details>`:''}</div>`;
}
async function loadMe(){me=await api('me');serverOffset=me.time-Date.now()/1000;shell();}
async function home(){
  hideEvent();deathUntil=0;closeModal();
  page='home';room=null;dirty=false;syncSabotage();await loadMe();
  if(me.current_room&&!me.host){await openRoom(me.current_room);return;}
  renderHome();
}
function renderHome(){
  shell();$('#main').innerHTML=`<div class="title-row"><h1>Моя комната</h1></div>
    ${me.rooms.length?`<div class="room-list">${me.rooms.map(r=>`<div class="room-card"><div><h2>${esc(r.name)}</h2><span class="muted small">${r.count} игроков · ${esc(r.code)}</span></div><div class="actions">${pill(r)}${btn('open-room','Открыть',`data-code="${esc(r.code)}"`,'small')}</div></div>`).join('')}</div>`:'<div class="card empty"><div class="crew-placeholder"></div><h2>Вы ещё не в комнате</h2><p>Создайте комнату или отправьте код в чате бота.<br>Здесь появятся настройки и сама игра.</p></div>'}
    <div class="actions" style="margin-top:20px">${btn('open-bot',info.mode==='demo'?'Открыть демочат бота':'Открыть чат бота','','primary wide')}</div>
    ${info.mode==='demo'?'<p class="footnote">Демочат использует тот же обработчик, что и настоящий Telegram-бот. Для реальной игры подключите токен и HTTPS.</p>':''}`;
}
function openBot(payload=''){
  if(info.mode==='demo')return chatPage();
  const url='https://t.me/'+info.bot_username+(payload?'?start='+encodeURIComponent(payload):'');
  if(tg()?.openTelegramLink)tg().openTelegramLink(url);else window.open(url,'_blank','noopener');
}
async function chatPage(){page='chat';dirty=false;if(!chatHistory.length)await sendChat('/start');else renderChat();}
function renderChat(){
  shell();$('#main').innerHTML=`<div class="title-row"><h1>Чат бота</h1><span class="pill gray">Локальная проверка</span></div><div class="chat-window">${chatHistory.map(m=>`<div class="chat-msg ${m.user?'from-user':'from-bot'}">${esc(m.text).replace(/\n/g,'<br>')}${m.demo_room?`<div class="chat-open">${btn('chat-open',m.demo_panel==='admin'?'Настроить комнату':'Открыть игру',`data-code="${esc(m.demo_room)}" data-panel="${esc(m.demo_panel)}"`,'wide')}</div>`:''}</div>`).join('')}</div><div class="chat-keyboard">${chatKeyboard.map(row=>`<div>${row.map(b=>btn('chat-text',esc(b.text),`data-text="${esc(b.text)}"`,'wide')).join('')}</div>`).join('')}</div><form id="chat-form" class="chat-input"><input name="text" aria-label="Сообщение боту" placeholder="Сообщение или код комнаты" required maxlength="200" autocomplete="off"><button class="btn primary" aria-label="Отправить боту">➤</button></form><p class="footnote">В настоящей игре эти сообщения и кнопки находятся в личном чате вашего Telegram-бота.</p>`;
}
async function sendChat(text){
  if(info.mode!=='demo')return openBot();
  text=String(text).trim();if(!text)return;
  page='chat';chatHistory.push({text,user:true});
  const result=await api('demo/chat',{text});
  for(const message of result.messages){chatHistory.push(message);if(message.reply_markup?.keyboard)chatKeyboard=message.reply_markup.keyboard;}
  await loadMe();renderChat();
}
async function openRoom(code){
  dirty=false;page='room';tab='player';setupStep=1;
  room=await api('rooms/'+encodeURIComponent(code));serverOffset=room.time-Date.now()/1000;
  if(room.admin && !room.me)tab='admin';
  if(room.admin)catalog=(await api('catalog')).stations;
  roomSignature=JSON.stringify({...room,time:0});renderRoom();window.scrollTo(0,0);
}
function roomHeader(){
  return `<div class="room-heading"><div><span class="muted small">${esc(room.code)} · ${room.players.length} игроков</span><h1>${esc(room.name)}</h1></div>${pill(room)}</div>`;
}
function renderRoom(){
  if(!room)return;shell();const openDetails=[...document.querySelectorAll('#main details[open]')].map(x=>x.querySelector('summary')?.textContent);
  $('#main').classList.toggle('player-screen',tab==='player'&&!!room.me);
  $('#main').innerHTML=testBanner()+commsTerminal()+roomHeader()+`${room.admin?`<div class="room-nav">${room.me?btn('room-tab','Моя игра','data-tab="player"',tab==='player'?'small selected':'small ghost'):''}${btn('room-tab','Admin-Панель','data-tab="admin"',tab==='admin'?'small selected':'small ghost')}${btn('room-tab','QR для печати','data-tab="print"',tab==='print'?'small selected':'small ghost')}</div>`:''}<div id="room-body">${tab==='admin'?adminBody():tab==='print'?printBody():playerBody()}</div>`;
  document.querySelectorAll('#main details').forEach(x=>{if(openDetails.includes(x.querySelector('summary')?.textContent))x.open=true;});
  updateTimers();syncEvents();syncSabotage();
}
function progressBlock(){let p=room.progress;return `<div class="progress-top"><span>Общий прогресс экипажа</span><b>${p.done} / ${p.total}</b></div><div class="progress"><span style="width:${p.total?100*p.done/p.total:0}%"></span></div>`;}
function playerBody(){
  const p=room.me;
  if(!p)return `<div class="card"><p>Вы — ведущий этой сессии.</p>${room.phase==='lobby'?btn('join-self','Участвовать в игре','','ghost'):''}${['gathering','discussion','voting'].includes(room.phase)?meetingBody():''}</div>`;
  if(room.phase==='lobby')return `<div class="card"><h2>Вы в игре</h2><p class="muted">Ждём, когда ведущий начнёт партию.</p>${btn('my-badge','Мой бейдж','','wide')}<details><summary>Игроки · ${room.players.length}</summary>${playersList()}</details>${btn('leave','Выйти из лобби','','ghost small')}</div>`;
  if(['gathering','discussion','voting'].includes(room.phase))return `${room.paused_at!=null?'<div class="notice">Игра на паузе.</div>':''}${meetingBody()}`;
  const ended=room.phase==='ended',active=room.phase==='running'&&room.paused_at==null;
  const todo=p.tasks.filter(t=>!t.done),done=p.tasks.filter(t=>t.done),crew=p.role==='crew',ghost=p.status==='dead';
  return `${ended?`<div class="notice"><h2>${({crew:'Победа экипажа',impostor:'Победа предателей',stopped:'Партия завершена'})[room.winner]||'Партия завершена'}</h2>${btn('show-result','Показать результат','','small')}</div>`:''}
  <div class="role-line"><div><span class="muted small">Ваша роль</span><strong class="${p.role==='impostor'?'red':''}">${esc(roleNames[p.role])}</strong></div>${btn('my-badge','Мой бейдж','','small ghost')}</div>
  ${ghost?`<div class="ghost-banner"><strong>ВАС УБИЛИ · ВЫ ПРИЗРАК</strong><p>Оставайтесь на своём месте. Не раскрывайте предателя и продолжайте выполнять задания здесь, без QR.${crew?' После каждого выполненного задания — перезарядка 60 секунд.':''}</p></div>`:p.status==='ejected'?'<div class="notice">Вы изгнаны. Не подсказывайте живым.</div>':''}
  ${room.paused_at!=null?'<div class="notice">Игра на паузе. Ждём ведущего.</div>':''}
  ${['gathering','discussion','voting'].includes(room.phase)?meetingBody():''}
  ${sabotageStatus()}
  ${active&&(!ghost||!crew)?`<div class="scan-block">${btn('scan-any','▣ &nbsp; Сканировать QR','','primary wide scan-button')}<p class="muted small">${crew?'Задание, бейдж, совещание или ремонт':'Бейдж игрока, совещание или ремонт'}</p></div>`:''}
  ${p.role==='impostor'&&p.status==='alive'&&active?`<div class="kill-meter"><span class="kill-icon">ϟ</span><div><b>УБИЙСТВО</b><span data-cooldown="${p.kill_ready}"></span><small>После готовности отсканируйте бейдж цели</small></div></div>`:''}
  ${sabotageControls()}
  ${crew?`<section class="task-section"><div class="card-header"><h2>Мои задания</h2><span class="muted small">${done.length} / ${p.tasks.length}</span></div>
  ${todo.map(t=>`<div class="task-row ${taskBlocked(t)?'sabotage-blocked':''} ${ghost&&p.ghost_ready>(room.paused_at??now())?'waiting':''}" ${ghost?'data-ghost-row':''}><span class="task-check"></span><span class="task-copy">${esc(t.name)}<small>${t.stage_count>1?`Этап ${t.stage+1} / ${t.stage_count} · `:''}${esc(t.stage_label||'')}</small>${taskBlocked(t)?'<small>Недоступно до устранения саботажа</small>':''}${t.ready_at>now()?`<small>Анализ: <span data-deadline="${t.ready_at}"></span></small>`:''}${ghost?`<small class="ghost-wait" data-ghost-countdown></small>`:''}</span>${ghost&&!ended?btn('ghost-task','Выполнить',`data-station="${esc(t.station)}" data-ghost-button ${!active||taskBlocked(t)||p.ghost_ready>now()||t.ready_at>now()?'disabled':''}`,'small primary'):''}</div>`).join('')||'<p class="muted small">Все ваши задания выполнены.</p>'}
  ${done.length?`<details class="completed"><summary>Выполнено · ${done.length}</summary>${done.map(t=>`<div class="task-row done"><span>✓</span><span>${esc(t.name)}</span></div>`).join('')}</details>`:''}</section>`:'<div class="impostor-objective"><b>Устраните экипаж</b><p>Выполнять задания нельзя. Используйте бейджи игроков и не выдавайте свою роль.</p></div>'}
  <div class="progress-section">${progressBlock()}</div>
  <details class="team-section"><summary>Игроки</summary>${playersList()}</details>
  ${info.mode==='demo'&&!me.test_controller&&active&&!ghost?`<details class="demo-tools"><summary>Демо: имитация сканирования</summary><p class="muted small">Только для проверки на компьютере.</p><div class="actions">${(crew?todo:[]).map(t=>btn('demo-scan',esc(t.name),`data-payload="s_${esc(t.station)}"`,'small')).join('')}${btn('demo-objects','Бейджи и совещание','','small ghost')}</div></details>`:''}`;
}
function playersList(){return room.players.map(p=>`<div class="row"><div class="row-title"><span class="avatar">${esc(p.name[0])}</span><div><b>${esc(p.name)}${p.id===me.user.id?' · Вы':''}</b><small>${room.phase==='ended'?esc(roleNames[p.role]):esc(statusNames[p.status])}</small></div></div>${room.admins.includes(p.id)?'<span class="pill gray">Admin</span>':''}</div>`).join('')||'<p class="muted small">Пока никто не присоединился.</p>';}
function meetingBody(){
  const m=room.meeting,p=room.me,caller=room.players.find(x=>x.id===m.caller),gathering=room.phase==='gathering';
  const canVote=room.phase==='voting'&&p?.status==='alive'&&!m.voted&&room.paused_at==null;
  return `<section class="meeting-room"><div class="eyebrow">${m.reason==='body'?'ОБНАРУЖЕНО ТЕЛО':'ЭКСТРЕННОЕ СОВЕЩАНИЕ'}</div><h2>${gathering?'Кто уже в комнате?':room.phase==='discussion'?'Обсуждение':'Кто предатель?'}</h2><p class="meeting-call">Вызвал: ${esc(caller?.name||'Ведущий')}${m.body?' · Тело: '+esc(room.players.find(x=>x.id===m.body)?.name||'участник'):''}</p>${gathering?`<div class="gather-count">${m.arrived_count} / ${m.required}</div><p>${p?.status==='dead'?'Наблюдайте за сбором. Оставайтесь на своём месте.':m.arrived_me?'Вы отметились. Ждём остальных живых игроков.':'Пройдите в главную комнату и отсканируйте QR совещания.'}</p>${p?.status==='alive'&&!m.arrived_me?btn('scan-any','Сканировать QR совещания','','primary wide'):''}${info.mode==='demo'&&p?.status==='alive'&&!m.arrived_me?btn('demo-arrival','Демо: сканировать QR совещания','','small ghost'):''}`:`<div class="timer" data-deadline="${m.deadline}"></div><p class="muted small">${room.phase==='discussion'?'Обсудите подозрения. После таймера начнётся голосование.':p?.status!=='alive'?'Вы наблюдатель. Погибшие не голосуют.':m.voted?'Ваш голос принят. Ждём остальных.':'Выберите игрока или пропустите голосование.'}</p>`}
  <div class="meeting-grid">${(m.participants||[]).map(x=>{const dead=['dead','ejected'].includes(x.status);return `<button class="meeting-player ${dead?'dead':''}" ${canVote&&!dead?`data-action="vote" data-player="${esc(x.id)}"`:'disabled'}><span class="avatar">${esc(x.name[0])}</span><span>${esc(x.name)}${x.id===me.user.id?' · Вы':''}<small>${dead?'ВЫБЫЛ · ГОЛОСОВАТЬ НЕЛЬЗЯ':gathering?'В КОМНАТЕ':'В ИГРЕ'}</small></span></button>`;}).join('')||'<div class="empty-seat">Пока никто не отметился по QR.</div>'}</div>
  ${canVote?btn('vote','Пропустить голосование','data-player="skip"','wide ghost'):''}${room.phase==='voting'?`<p class="muted small">Голосов: ${m.vote_count} / ${m.required}</p>`:''}</section>`;
}
function adminPlayers(){
  return room.players.map(p=>`<div class="row"><div><b>${esc(p.name)}</b><div class="muted small">${p.role?esc(roleNames[p.role])+' · ':''}${esc(statusNames[p.status])}${room.admins.includes(p.id)?' · Admin':''}</div></div>${btn('player-tools','Изменить',`data-player="${esc(p.id)}"`,'small ghost')}</div>`).join('')||'<p class="muted small">Пока никто не присоединился.</p>';
}
function adminBody(){
  if(room.phase==='lobby')return setupBody()+testPlayersBody();
  const ended=room.phase==='ended',paused=room.paused_at!=null;
  return `<div class="card"><div class="card-header"><h2>${ended?'Партия завершена':'Управление игрой'}</h2>${ended?'':btn('control',paused?'Продолжить':'Пауза',`data-control="${paused?'resume':'pause'}"`,'primary')}</div><p class="muted small">Вы — ведущий. Здесь видны роли игроков.</p>${progressBlock()}
  ${['gathering','discussion','voting'].includes(room.phase)?`<div class="meeting-admin"><b>${esc(phaseNames[room.phase])}</b>${room.phase==='gathering'?`<span>Прибыли ${room.meeting.arrived_count} / ${room.meeting.required}</span>`:`<span class="timer" data-deadline="${room.meeting.deadline}"></span>`}${btn('control',room.phase==='gathering'?'Начать обсуждение без всех':room.phase==='discussion'?'Начать голосование':'Подвести итог',`data-control="next_phase" ${paused?'disabled':''}`,'small')}</div>`:''}
  ${ended?btn('create-room','Новая сессия','','primary wide'):''}</div>
  ${sabotageStatus()}${room.sabotage?btn('control','Устранить саботаж','data-control="clear_sabotage"','wide'):''}
  <div class="card"><h2>Игроки</h2>${adminPlayers()}</div>${testPlayersBody()}
  ${!ended?`<details class="card"><summary>Другие действия</summary><div class="actions details-content">${btn('undo','Отменить последнее','','ghost')}${['gathering','discussion','voting'].includes(room.phase)?`${btn('control','Отменить совещание','data-control="cancel_meeting"','ghost')}${room.phase!=='gathering'?btn('control','Перезапустить голосование','data-control="restart_vote"','ghost'):''}`:''}${btn('end-game','Завершить партию','','danger')}</div></details>`:''}
  <details class="card"><summary>Журнал действий</summary><div class="details-content">${(room.events||[]).slice(0,30).map(e=>`<div class="event ${e.undone?'undone':''}"><time>${new Date(e.at*1000).toLocaleTimeString('ru',{hour:'2-digit',minute:'2-digit'})}</time><span>${esc(e.action)}</span></div>`).join('')}</div></details>`;
}
function setupBody(){
  const ready=room.players.length>=3&&room.players.length>2*room.settings.impostors&&room.stations.length>=room.settings.tasks;
  return `<div class="setup-steps">${['Задания','Игроки','Старт'].map((name,i)=>`<button class="step-button ${setupStep===i+1?'active':''}" data-action="setup-step" data-step="${i+1}"><span>${i+1}</span>${name}</button>`).join('')}</div>
  ${setupStep===1?lobbySettings():setupStep===2?`<div class="card"><h2>Пригласите игроков</h2><p class="muted">Передайте код <b class="join-code">${esc(room.code)}</b> или покажите QR.</p>${btn('room-qr','Показать приглашение','','primary wide')}<div class="divider"></div>${adminPlayers()}${info.mode==='demo'&&me.host?`<details class="demo-tools"><summary>Проверить без Telegram</summary>${btn('populate','Добавить демоигроков','','wide')}</details>`:''}<div class="wizard-bottom">${btn('setup-step','Назад','data-step="1"','ghost')}${btn('setup-step','Далее →','data-step="3"','primary')}</div></div>`:
  `<div class="card"><h2>Подготовьте площадку</h2><p class="muted">Разместите QR заданий, совещания и ремонта. Раздайте игрокам бейджи.</p><div class="summary-row"><span>Игроков</span><b>${room.players.length}</b></div><div class="summary-row"><span>Станций с заданиями</span><b>${room.stations.length}</b></div><div class="summary-row"><span>Заданий каждому</span><b>${room.settings.tasks}</b></div>${btn('print-all','Показать все QR для печати','','wide')}<p class="muted small" style="margin-top:16px">${ready?'Всё готово. При старте игроки получат роли и задания.':'Нужны минимум 3 игрока, предателей меньше половины и достаточно выбранных станций.'}</p><div class="wizard-bottom">${btn('setup-step','Назад','data-step="2"','ghost')}${btn('start-game','Начать игру',ready?'':'disabled','primary')}</div></div>`}`;
}
function lobbySettings(){
  const s=room.settings;
  return `<form id="room-settings" class="card"><div class="card-header"><h2>Выберите задания</h2>${btn('new-station','＋ Добавить','','small ghost')}</div><p class="muted small">Отметьте станции, которые будут в этой сессии.</p>
    ${catalog.map(st=>`<label class="check-row"><input type="checkbox" name="stations" value="${esc(st.code)}" ${room.stations.some(x=>x.code===st.code)?'checked':''}><span><b>${esc(st.name)}</b><small>${esc(kindNames[st.kind])}</small></span></label>`).join('')||'<p class="muted">Создайте первую станцию кнопкой «Добавить».</p>'}
    <div class="divider"></div>${input('tasks','Сколько заданий получит каждый',s.tasks,'number','min="1" max="30" required')}
    <details><summary>Правила игры</summary><div class="form-grid details-content">${input('name','Название сессии',room.name,'text','maxlength="60" required')}${input('impostors','Предателей',s.impostors,'number','min="1" max="10" required')}${input('cooldown','Перезарядка убийства, сек.',s.cooldown,'number','min="5" max="600" required')}${input('discussion','Обсуждение, сек.',s.discussion,'number','min="0" max="600" required')}${input('voting','Голосование, сек.',s.voting,'number','min="10" max="600" required')}${input('emergencies','Совещаний на игрока',s.emergencies,'number','min="0" max="5" required')}</div></details>
    ${info.mode==='demo'&&me.host&&!catalog.length?`<details class="demo-tools"><summary>Проверить без Telegram</summary>${btn('populate','Подготовить демопартию','','wide')}</details>`:''}
    <div class="wizard-bottom"><span id="unsaved" class="muted small"></span><button class="btn primary">Сохранить и далее →</button></div></form>`;
}
function printBody(){
  return `<div class="card"><h2>QR для площадки</h2><p class="muted small">Станции и бейджи сохраняются для следующих игр.</p>${info.mode==='demo'?'<div class="notice">Сейчас коды ведут в локальную демонстрацию. Для настоящей игры сначала подключите Telegram.</div>':''}${btn('print-all','Все QR одним листом','','primary wide')}<details style="margin-top:16px"><summary>Выбрать, что печатать</summary><div class="actions details-content">${btn('print-stations','Задания','','small')}${btn('print-meeting','Совещание','','small')}${btn('print-badges','Бейджи','','small')}${btn('print-sabotage','Ремонт саботажей','','small')}${btn('room-qr','Приглашение','','small')}</div></details></div><div id="qr-preview"></div>`;
}
async function catalogPage(){page='catalog';dirty=false;catalog=(await api('catalog')).stations;renderCatalog();}
function renderCatalog(){
  shell();$('#main').innerHTML=`<div class="title-row"><div><h1>Станции</h1><p class="muted small">Создайте один раз, выбирайте для разных сессий.</p></div>${btn('new-station','Добавить','','primary')}</div><div class="card">${catalog.map(st=>`<div class="row"><div><b>${esc(st.name)}</b><div class="muted small">${esc(kindNames[st.kind])}</div></div>${btn('station-qr','QR',`data-station="${esc(st.code)}"`,'small ghost')}</div>`).join('')||'<p class="muted">Пока нет станций.</p>'}</div>`;
}
async function settingsPage(){
  page='settings';dirty=false;const s=await api('settings');shell();
  $('#main').innerHTML=`<h1>Подключение</h1><div class="card"><h2>${s.mode==='telegram'?'Telegram + Cloudflare':'Локальная демонстрация'}</h2><p class="muted">Обычный запуск: START.cmd. Скрипт сам подключает бота и создаёт публичный HTTPS-адрес.</p><div class="summary-row"><span>Бот</span><b>${esc(s.bot_username?'@'+s.bot_username:'—')}</b></div><div class="summary-row"><span>Состояние</span><b>${esc(s.bot.detail||s.bot.state)}</b></div>${s.public_url?`<p class="small" style="overflow-wrap:anywhere">Mini App: ${esc(s.public_url)}</p>`:''}<p class="muted small">Для смены токена измените BOT_TOKEN в файле .env либо запустите START.cmd --setup. После перезапуска откройте игру новой кнопкой бота.</p></div><details class="card"><summary>Владельцы сервера</summary><form id="server-settings" class="details-content"><input type="hidden" name="mode" value="${esc(s.mode)}"><input type="hidden" name="bot_username" value="${esc(s.bot_username)}"><input type="hidden" name="public_url" value="${esc(s.public_url)}"><input type="hidden" name="app_short_name" value="${esc(s.app_short_name||'')}"><label class="label" for="admin-ids">Telegram ID, через запятую</label><input id="admin-ids" name="admin_ids" value="${esc(s.admin_ids.join(', '))}" placeholder="123456789"><p class="muted small">Узнать ID: /whoami в чате бота. Создатель комнаты автоматически получает её Admin-Панель. Эти ID нужны только для доступа ко всем комнатам сервера. Менять их можно на компьютере сервера.</p><button class="btn primary wide" ${info.local?'':'disabled'}>Сохранить и применить после перезапуска</button></form></details><p class="muted small">Отдельная настройка Main Mini App в BotFather не требуется. <a href="/guide" target="_blank" rel="noreferrer">Инструкция ↗</a></p>`;
}
async function roomAction(action,data={}){const result=await api(`rooms/${room.code}/${action}`,data);dirty=false;room=await api('rooms/'+room.code);serverOffset=room.time-Date.now()/1000;roomSignature=JSON.stringify({...room,time:0});renderRoom();return result;}
async function loginDemo(id){chatHistory=[];chatKeyboard=[];const result=await api('auth/demo',{id});setToken(result.token);closeModal();await home();await consumeStart();}
async function switchDemo(){modal(`<h2>Сменить участника</h2><p class="muted small">Доступно только на этом компьютере в деморежиме. Партия продолжит идти.</p><div class="demo-select">${[1,2,3,4].map(n=>btn('demo-login','Игрок '+n,`data-player="demo_${n}"`)).join('')}</div><div class="divider"></div><p class="muted small">Для возврата в панель ведущего:</p>${btn('restore-owner','Вернуться к ведущему','','wide ghost')}`);}
async function showQR(items,title){
  const cards=[];
  for(const item of items){const qr=await api('qr',{payload:item.payload});cards.push({...item,...qr});}
  printItems=cards;
  const content=`<h2>${esc(title)}</h2>${info.mode==='demo'?'<div class="note warn">Тестовые коды для этого компьютера. Перед печатью для площадки переключитесь на Telegram.</div>':''}<div class="actions" style="margin-top:15px">${btn('print','Печатать / сохранить PDF','','primary')}</div><div class="print-preview">${cards.map((q,i)=>`<div class="qr-card"><img src="${q.image}" alt="QR: ${esc(q.name)}"><b>${esc(q.name)}</b><span class="small">${esc(q.subtitle||'')}</span><br><a href="${esc(q.url)}" target="_blank" rel="noreferrer">Открыть ссылку ↗</a><br><a download="qr-${i+1}.svg" href="${q.image}">Скачать SVG</a></div>`).join('')}</div>`;
  if(page==='room'&&tab==='print')$('#qr-preview').innerHTML='<div class="card" style="margin-top:20px">'+content+'</div>';else modal(content);
}
function stationQrs(stations){return stations.flatMap(s=>{const stages=s.stages||info.task_types?.[s.kind]?.stages||[s.name];return stages.map((label,i)=>({payload:'s_'+s.code+(stages.length>1?'_p'+(i+1):''),name:s.name,subtitle:stages.length>1?`Этап ${i+1} / ${stages.length} · ${label}`:kindNames[s.kind]}));});}
function printSheet(){
  const w=window.open('','_blank');if(!w){toast('Браузер заблокировал окно печати. Разрешите всплывающие окна.',true);return;}
  const html=`<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>QR-коды · Смена</title><style>@page{size:A4;margin:12mm}body{font:14px system-ui;color:#111;margin:0}header{margin-bottom:18px;font-size:11px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12mm}.q{break-inside:avoid;border:1px dashed #aaa;text-align:center;padding:8mm}.q img{width:48mm;height:48mm}.q h2{font-size:18px;margin:7px 0}.q p{font-size:11px;margin:4px;overflow-wrap:anywhere}@media print{button{display:none}}</style></head><body><header>СМЕНА / AMONG US LIVE ${info.mode==='demo'?' · ДЕМО — ТОЛЬКО ЭТОТ КОМПЬЮТЕР':''}</header><button onclick="window.print()">Печатать / сохранить PDF</button><div class="grid">${printItems.map(q=>`<section class="q"><img src="${q.image}" alt="QR"><h2>${esc(q.name)}</h2><p>${esc(q.subtitle||'')}</p><p>${esc(q.url)}</p></section>`).join('')}</div></body></html>`;
  w.document.write(html);w.document.close();w.onload=()=>setTimeout(()=>w.print(),250);
}
function parsePayload(raw){
  let value=String(raw).trim();try{const u=new URL(value);value=u.searchParams.get('startapp')||u.searchParams.get('start')||'';}catch{}
  if(!/^[sbjmx]_[A-Za-z0-9_-]{6,64}$/.test(value))throw new Error('Это не QR этой игры');return value;
}
async function scan(){
  const app=tg();
  if(app?.initData&&app.isVersionAtLeast?.('6.4')){
    app.showScanQrPopup({text:'Наведите камеру на QR задания, бейджа, совещания или системы'},value=>{app.closeScanQrPopup();handlePayload(value).catch(e=>toast(e.message,true));return true;});
  }else modal(`<h2>Сканирование</h2><p>Откройте камеру телефона и отсканируйте QR на объекте. Ссылка откроет игру в Telegram.</p>${info.mode==='demo'?'<p class="muted small">На компьютере используйте раздел «Демо: имитация сканирования».</p>':'<p class="muted small">Встроенный сканер доступен в Telegram на телефоне.</p>'}`);
}
async function handlePayload(raw){
  const value=parsePayload(raw),id=value.slice(2);
  if(value.startsWith('j_')){if(info.mode==='telegram'){openBot(value);return;}await api('join',{code:id});await loadMe();await openRoom(id);toast('Вы в сессии');return;}
  await loadMe();
  if(!room||room.code!==me.current_room){if(me.current_room)await openRoom(me.current_room);else throw new Error('Сначала войдите в сессию');}
  const result=await api(`rooms/${room.code}/scan`,{payload:value});
  if(result.kind==='task'){await openTask(id,result.receipt);return;}
  if(result.actions.includes('sabotage_open')){await openRepair(result.receipt);return;}
  closeModal();scannedObject={...result,roomCode:room.code};
  if(!result.actions.length){modal(`<h2>${esc(result.name)}</h2><p class="muted">Игрок жив. Сейчас нет доступных действий.</p>`);return;}
  if(result.actions.includes('meeting_join')){await roomAction('meeting_join',{receipt:result.receipt});closeModal();toast('Вы отметились в главной комнате');return;}
  const action=result.actions[0], titles={kill:'Устранить игрока?',report:'Тело обнаружено',emergency:'Вызвать совещание?'};
  const texts={kill:esc(result.name),report:`${esc(result.name)} погиб. Сообщить остальным?`,emergency:'Все игроки получат уведомление и соберутся на обсуждение.'};
  const labels={kill:'Подтвердить убийство',report:'Сообщить о теле',emergency:'Вызвать совещание'};
  modal(`<p class="scan-confirmed">✓ QR отсканирован</p><h2>${titles[action]}</h2><p>${texts[action]}</p>${btn('confirm-scan',labels[action],`data-intent="${action}"`,action==='kill'?'danger wide':'primary wide')}<p class="muted small" style="margin-top:12px">Подтвердите в течение 30 секунд.</p>`);
}
async function demoObjects(){
  const items=await api(`rooms/${room.code}/demo_objects`,{});
  modal(`<h2>Проверка QR</h2><p class="muted small">Имитация сканирования на компьютере.</p><div class="demo-object-list">${items.map(x=>btn('demo-scan',esc(x.name),`data-payload="${esc(x.payload)}"`,'wide')).join('')}</div>`);
}
async function consumeStart(){
  const params=new URLSearchParams(location.search), requested=params.get('room'), panel=params.get('panel');
  const payload=tg()?.initDataUnsafe?.start_param||params.get('tgWebAppStartParam')||params.get('start');
  if(!me)return;
  if(requested){await openRoom(requested);if(panel==='admin'&&room.admin){tab='admin';renderRoom();}history.replaceState(null,'',location.pathname);return;}
  if(payload){history.replaceState(null,'',location.pathname);await handlePayload(payload);}
}
async function openTask(station,receipt,ghost=false){
  const task=await api(`rooms/${room.code}/${ghost?'ghost_task_open':'task_open'}`,ghost?{station}:{receipt});
  closeModal();const puzzle={...task,stationCode:task.station_code||station,roomCode:room.code,controller:null};currentPuzzle=puzzle;
  modal(`<div class="task-title"><span class="task-bolt">ϟ</span><div><span class="eyebrow">${esc(kindNames[task.station.kind])}</span><h2>${esc(task.station.name)}</h2></div></div><div class="task-stage-heading"><span>${esc(task.stage_label||'')}</span><span class="stage-pill">Этап ${(task.stage||0)+1} / ${task.stage_count||1}</span></div><div id="puzzle"></div><div class="task-sab-note" role="status"></div>`);
  const root=$('#puzzle');
  puzzle.controller=GameTasks.mount(root,task,answer=>api(`rooms/${puzzle.roomCode}/task_finish`,{station:puzzle.stationCode,id:puzzle.id,epoch:puzzle.epoch,answer}));
  syncSabotage();
  root.addEventListener('task-complete',async()=>{if(currentPuzzle!==puzzle)return;closeModal();await refreshRoom(true);},{once:true});
}
function playerTools(pid){
  const p=room.players.find(x=>x.id===pid);if(!p)return;
  const editable=room.phase==='running'&&room.paused_at!=null;
  modal(`<h2>${esc(p.name)}</h2><p class="muted small">${esc(roleNames[p.role]||'Роль ещё не выдана')} · ${esc(statusNames[p.status])}</p><div class="actions">${room.phase==='lobby'?btn('player-control','Убрать из лобби',`data-control="kick" data-player="${esc(pid)}"`,'danger'):''}${editable?`${btn('player-control',p.status==='alive'?'Исключить':'Восстановить',`data-control="${p.status==='alive'?'eject':'revive'}" data-player="${esc(pid)}"`)}${btn('player-control','Сменить роль',`data-control="role" data-player="${esc(pid)}" data-role="${p.role==='crew'?'impostor':'crew'}"`,'ghost')}`:''}${pid!==room.owner&&!p.bot?btn('player-control',room.admins.includes(pid)?'Снять Admin':'Назначить Admin',`data-control="${room.admins.includes(pid)?'revoke_admin':'grant_admin'}" data-player="${esc(pid)}"`,'ghost'):''}</div>${!editable&&room.phase!=='lobby'?'<div class="note warn" style="margin-top:15px">Для изменения роли, жизни и заданий поставьте обычную игру на паузу. Во время собрания сначала отмените его.</div>':''}${p.tasks?.length?'<div class="divider"></div><h3>Личные задания</h3>'+p.tasks.map(t=>`<div class="row"><span class="small">${esc(t.name)} · ${t.done?'Готово':'Не выполнено'}</span>${editable?btn('task-toggle',t.done?'Сбросить':'Засчитать',`data-player="${esc(pid)}" data-station="${esc(t.station)}" data-done="${!t.done}"`,'small ghost'):''}</div>`).join(''):''}`);
}

function taskBlocked(t){return !!t&&!!room?.sabotage?.blocked.includes(t.kind||room.stations.find(s=>s.code===t.station)?.kind);}
function testBanner(){
  if(me.test_controller)return `<div class="test-controller"><b>ТЕСТ · ${esc(me.user.name)}</b><p>Вы управляете ботом. Его автоматические действия отключены.</p><div class="actions">${btn('return-host','Вернуться к хосту','','small primary')}${btn('test-objects','Тестовые QR','','small ghost')}</div></div>`;
  return room.test_mode?'<div class="test-mode-banner">Тестовая сессия · в комнате есть боты</div>':'';
}
function commsTerminal(){
  if(room.phase!=='running'||room.sabotage?.kind!=='comms')return '';
  const busy=room.sabotage.busy?.comms;
  return `<section class="comms-terminal" aria-label="Терминал связи"><div class="terminal-window-title"><span>Игровой терминал · C:\\SHIP\\SYSTEM32</span><span aria-hidden="true">— □ ×</span></div><div class="terminal-output"><p>C:\\SHIP&gt; connection --status</p><p>[ОШИБКА] ОБНАРУЖЕНО ВТОРЖЕНИЕ</p><h2>СИСТЕМА ВЗЛОМАНА</h2><p>Связь с сервером перехвачена.<br>Нужно отбить атаку.</p><p>${busy?'[ЗАНЯТО] Другой игрок восстанавливает связь.':'[ДЕЙСТВИЕ] Найдите серверную панель, отсканируйте её QR и составьте маршрут от А до Б.'}</p>${room.me?.status==='alive'&&room.paused_at==null?btn('scan-any','СКАНИРОВАТЬ QR СЕРВЕРА',busy?'disabled':'','primary wide'):''}<p class="terminal-prompt">C:\\SHIP&gt; _</p></div></section>`;
}
function sabotageControls(){
  if(room.me?.role!=='impostor'||room.phase!=='running')return '';
  return `<section class="sabotage-meter sabotage-controls"><div class="sabotage-controls-heading"><b>САБОТАЖ</b><span data-sabotage-countdown></span></div><small data-sabotage-hint>Запуск из любого места</small><div class="sabotage-buttons">${[['comms','Связь','Взлом системы'],['oxygen','Кислород','45 секунд'],['reactor','Реактор','45 секунд']].map(([kind,name,hint])=>btn('launch-sabotage',`<b>${name}</b><small>${hint}</small>`,`data-kind="${kind}"`,'sabotage-launch')).join('')}</div></section>`;
}
function sabotageStatus(){
  const s=room.sabotage;if(!s)return '';
  const detail=s.kind==='oxygen'?'Почините любую одну панель: А или Б':s.kind==='reactor'?`Сенсоры: ${Object.values(s.held).filter(Boolean).length} / 2`:s.busy?.comms?'Ремонт выполняет другой игрок':'Соедините точки А и Б на серверной панели';
  return `<div class="sabotage-status"><div><b>САБОТАЖ · ${esc(s.name)}</b><small>${detail}${s.kind==='oxygen'&&Object.values(s.busy||{}).some(Boolean)?' · Ремонт занят другим игроком':''}. Отсканируйте QR ремонта.</small></div>${s.deadline?`<strong data-deadline="${s.deadline}"></strong>`:''}</div>`;
}
function sabotageQrs(){return (room.sabotage_qrs||[]).map(q=>({...q,subtitle:q.kind==='oxygen'?'КИСЛОРОД · достаточно одной панели А или Б':'РЕМОНТ · живые игроки'}));}
function testPlayersBody(){
  const bots=room.players.filter(p=>p.bot),lobby=room.phase==='lobby',active=room.phase==='running'&&room.paused_at==null;
  return `<details class="card" ${lobby&&bots.length?'open':''}><summary>Тестовые игроки${bots.length?' · '+bots.length:''}</summary><div class="details-content"><p class="muted small">Боты позволяют проверить игру без дополнительных Telegram-аккаунтов. В автоматическом режиме они приходят на совещания и голосуют пропуском.</p>${lobby?`<div class="actions">${btn('add-bots','＋ 1 бот','data-count="1"','small')}${btn('add-bots','＋ 4 бота','data-count="4"','small primary')}</div>`:''}${room.test_mode&&room.me?btn('test-objects','Тест: выбрать QR для себя','','small ghost'):''}${bots.map(p=>`<div class="test-bot-card"><b>${esc(p.name)} · ${esc(roleNames[p.role]||'В лобби')}</b><small>${esc(statusNames[p.status])} · ${p.auto?'Авто':'Ручное управление'}</small><div class="actions">${btn('bot-pov','Играть за бота',`data-player="${esc(p.id)}"`,'small primary')}${btn('bot-action',p.auto?'Отключить авто':'Включить авто',`data-player="${esc(p.id)}" data-command="auto"`,'small ghost')}${lobby?btn('bot-action','Убрать',`data-player="${esc(p.id)}" data-command="remove"`,'small ghost'):''}${active&&p.role==='crew'?btn('bot-action','Засчитать задание',`data-player="${esc(p.id)}" data-command="task"`,'small ghost'):''}${active&&room.sabotage&&p.status==='alive'?btn('bot-action',room.sabotage.kind==='reactor'?'Держать сенсор 15 с':'Помочь с ремонтом',`data-player="${esc(p.id)}" data-command="repair"`,'small ghost'):''}</div></div>`).join('')}</div></details>`;
}
async function enterBot(player){
  const code=room.code,hostToken=token;closeModal();
  const result=await api(`rooms/${code}/bot_session`,{player});
  sessionStorage.setItem('aul_test_host',JSON.stringify({token:hostToken,code}));setToken(result.token);hideEvent();deathUntil=0;await loadMe();await openRoom(code);
}
async function returnHost(){
  const saved=JSON.parse(sessionStorage.getItem('aul_test_host')||'null');if(!saved)throw new Error('Откройте игру заново из чата хоста');
  closeModal();setToken(saved.token);sessionStorage.removeItem('aul_test_host');hideEvent();deathUntil=0;await loadMe();await openRoom(saved.code);tab='admin';renderRoom();
}
async function testObjects(){
  const items=await api(`rooms/${room.code}/test_objects`,{});
  modal(`<h2>Тестовые QR</h2><p class="muted small">Выберите объект, чтобы проверить его сканирование. Эта возможность доступна хосту и управляемым ботам тестовой сессии.</p><div class="demo-object-list">${items.map(x=>btn('test-scan',esc(x.name),`data-payload="${esc(x.payload)}"`,'wide')).join('')}</div>`);
}
async function openRepair(receipt){
  const grant=await api(`rooms/${room.code}/sabotage_open`,{receipt});closeModal();
  const code=room.code,sessionToken=token,repair={...grant,controller:null};currentRepair=repair;$('#modal').classList.toggle('comms-dialog',grant.kind==='comms');
  modal(`<div class="task-title"><span class="task-bolt">!</span><div><span class="eyebrow">АВАРИЙНАЯ ПАНЕЛЬ</span><h2>${esc(grant.name)}</h2></div></div><div id="repair" class="sabotage-repair"></div>`);
  // Keep the original session for an asynchronous sensor release when returning to the host.
  const call=async(action,data)=>{
    const response=await fetch(`/api/rooms/${code}/${action}`,{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+sessionToken},keepalive:action==='repair_release',body:JSON.stringify({id:grant.id,ticket:grant.ticket,...data})});
    const value=await response.json();if(!response.ok)throw new Error(value.error||'Панель недоступна');return value;
  };
  repair.controller=SabotageUI.mount($('#repair'),grant,call,async()=>{if(currentRepair!==repair)return;closeModal();await refreshRoom(true);});syncSabotage();updateTimers();
}

async function handleClick(b){
  const a=b.dataset.action;
  if(a==='nav'){closeModal();if(b.dataset.page==='home')await home();if(b.dataset.page==='catalog')await catalogPage();if(b.dataset.page==='settings')await settingsPage();if(b.dataset.page==='chat')await chatPage();}
  else if(a==='dismiss-result'){sessionStorage.setItem(resultKey(),'1');hideEvent();}
  else if(a==='show-result'){sessionStorage.removeItem(resultKey());syncEvents();}
  else if(a==='observe-meeting'){sessionStorage.setItem(meetingKey(),'1');hideEvent();}
  else if(a==='event-admin'){meetingAdminBypass=room.meeting.id;tab='admin';hideEvent();renderRoom();}
  else if(a==='ghost-task')await openTask(b.dataset.station,null,true);
  else if(a==='demo-arrival'){const objects=await api(`rooms/${room.code}/demo_objects`,{});await handlePayload(objects.find(o=>o.payload.startsWith('m_')).payload);}
  else if(a==='home')await home();
  else if(a==='settings')await settingsPage();
  else if(a==='create-room'||a==='join-room'||a==='open-bot')await openBot();
  else if(a==='chat-text')await sendChat(b.dataset.text);
  else if(a==='chat-open'){await loadMe();await openRoom(b.dataset.code);if(b.dataset.panel==='admin'&&room.admin){tab='admin';renderRoom();}}
  else if(a==='open-room')await openRoom(b.dataset.code);
  else if(a==='room-tab'){tab=b.dataset.tab;dirty=false;renderRoom();}
  else if(a==='setup-step'){setupStep=+b.dataset.step;renderRoom();}
  else if(a==='new-station')modal(`<h2>Новая станция</h2><p class="muted small">Одинаковую механику можно использовать на нескольких станциях. У каждой будет свой постоянный QR.</p><form id="create-station">${input('name','Название станции','','text','required maxlength="60" placeholder="Например, резервное питание"')}<div class="field"><label class="label" for="kind">Тип активности</label><select id="kind" name="kind">${Object.entries(kindNames).filter(([k])=>k!=='tuning').map(([k,n])=>`<option value="${k}" ${b.dataset.kind===k?'selected':''}>${n}</option>`).join('')}</select></div><button class="btn primary wide">Создать станцию</button></form>`);
  else if(a==='populate'){await api('demo/populate',{code:room.code});catalog=(await api('catalog')).stations;await refreshRoom(true);toast('Демопартия подготовлена. Можно запускать');}
  else if(a==='start-game')modal(`<h2>Начать партию?</h2><p class="muted small">Роли и личные задания будут выданы ${room.players.length} участникам. Сначала сохраните настройки и раздайте бейджи.</p>${btn('confirm-start','Выдать роли и запустить','','primary wide')}`);
  else if(a==='confirm-start'){await roomAction('start');closeModal();toast('Партия началась');}
  else if(a==='control'){await roomAction('control',{action:b.dataset.control});toast('Состояние обновлено');}
  else if(a==='undo')modal(`<h2>Отменить последнее изменение?</h2><p class="muted small">Восстановится всё состояние сессии перед последним игровым изменением. Игра будет на паузе. Сообщения, уже отправленные в Telegram, останутся.</p>${btn('confirm-undo','Отменить и поставить на паузу','','danger wide')}`);
  else if(a==='confirm-undo'){await roomAction('undo');closeModal();toast('Изменение отменено. Проверьте состояние и снимите паузу');}
  else if(a==='end-game')modal(`<h2>Завершить партию?</h2><p class="muted small">Игроки увидят роли и сообщение о завершении. Для следующей игры создайте новую сессию.</p>${btn('confirm-end','Завершить партию','','danger wide')}`);
  else if(a==='confirm-end'){await roomAction('control',{action:'end'});closeModal();}
  else if(a==='join-self'){if(info.mode==='telegram')await openBot('j_'+room.code);else{await api('join',{code:room.code});await refreshRoom(true);}}
  else if(a==='leave'){await api(`rooms/${room.code}/leave`,{});await home();}
  else if(a==='demo-login')await loginDemo(b.dataset.player);
  else if(a==='switch')await switchDemo();
  else if(a==='restore-owner'){const owner=sessionStorage.getItem('aul_owner');if(!owner)throw new Error('Откройте ссылку владельца из окна запуска');setToken(owner);closeModal();await home();}
  else if(a==='room-qr')await showQR([{payload:'j_'+room.code,name:room.name,subtitle:'Присоединиться к сессии · '+room.code}],'Приглашение в сессию');
  else if(a==='station-qr'){const st=catalog.find(s=>s.code===b.dataset.station);await showQR(stationQrs([st]),'QR станции');}
  else if(a==='my-badge')await showQR([{payload:'b_'+room.me.badge,name:me.user.name,subtitle:'Личный бейдж игрока'}],'Мой бейдж');
  else if(a==='print-sabotage')await showQR(sabotageQrs(),'Ремонт саботажей');
  else if(a==='add-bots'){await roomAction('add_bots',{count:+b.dataset.count});toast('Тестовые игроки добавлены');}
  else if(a==='bot-action'){await roomAction('bot_action',{player:b.dataset.player,action:b.dataset.command});toast('Команда выполнена');}
  else if(a==='bot-pov')await enterBot(b.dataset.player);
  else if(a==='return-host')await returnHost();
  else if(a==='test-objects')await testObjects();
  else if(a==='test-scan')await handlePayload(b.dataset.payload);
  else if(a==='print-stations')await showQR(stationQrs(room.stations),'Станции сессии');
  else if(a==='print-badges')await showQR(room.players.map(p=>({payload:'b_'+p.badge,name:p.name,subtitle:'Личный бейдж игрока'})),'Бейджи участников');
  else if(a==='print')printSheet();
else if(a==='scan-any')await scan();
  else if(a==='demo-scan'){
    if(info.mode!=='demo')throw new Error('Демо недоступно');
    await handlePayload(b.dataset.payload);
  }
  else if(a==='demo-objects')await demoObjects();
  else if(a==='launch-sabotage'){if(sabotageBusy)return;sabotageBusy=true;updateTimers();try{await roomAction('sabotage_start',{kind:b.dataset.kind});toast('Саботаж запущен');}finally{sabotageBusy=false;updateTimers();}}
  else if(a==='confirm-scan'){
    const obj=scannedObject;
    if(!obj||obj.expires<now()){closeModal();throw new Error('Время истекло. Отсканируйте QR ещё раз');}
    await roomAction(b.dataset.intent,{receipt:obj.receipt});scannedObject=null;closeModal();toast('Готово');
  }
  else if(a==='print-meeting')await showQR([{payload:'m_'+room.meeting_code,name:'Совещание · главная комната',subtitle:'В обычной игре — вызов; во время сбора — отметка прибытия'}],'Станция совещания');
  else if(a==='print-all'){
    tab='print';renderRoom();
    const items=[...stationQrs(room.stations),...sabotageQrs(),{payload:'m_'+room.meeting_code,name:'Совещание · главная комната',subtitle:'В обычной игре — вызов; во время сбора — отметка прибытия'},...room.players.map(p=>({payload:'b_'+p.badge,name:p.name,subtitle:'Бейдж игрока'}))];
    await showQR(items,'Коды для площадки');
  }
  else if(a==='vote'){await roomAction('vote',{meeting:room.meeting.id,target:b.dataset.player});toast('Голос принят');}
  else if(a==='player-tools')playerTools(b.dataset.player);
  else if(a==='player-control'){await roomAction('control',{action:b.dataset.control,player:b.dataset.player,role:b.dataset.role});closeModal();toast('Игрок обновлён');}
  else if(a==='task-toggle'){await roomAction('control',{action:'task_reset',player:b.dataset.player,station:b.dataset.station,done:b.dataset.done==='true'});playerTools(b.dataset.player);}

}
document.addEventListener('click',async e=>{const b=e.target.closest('[data-action]');if(!b||b.disabled)return;e.preventDefault();b.disabled=true;try{await handleClick(b);}catch(err){toast(err.message,true);}finally{b.disabled=false;updateTimers();}});
document.addEventListener('submit',async e=>{
  const f=e.target;if(!f.id)return;e.preventDefault();const data=new FormData(f);const button=f.querySelector('button[type="submit"],button:not([type])');if(button?.disabled)return;if(button)button.disabled=true;
  try{
    if(f.id==='owner-login'){const result=await api('auth/local',{key:String(data.get('key')).replace(/^.*#setup=/,'')});setToken(result.token);sessionStorage.setItem('aul_owner',token);await home();}
    if(f.id==='create-station'){await api('stations',{name:data.get('name'),kind:data.get('kind')});closeModal();catalog=(await api('catalog')).stations;if(page==='room')renderRoom();else await catalogPage();toast('Станция создана. Её QR останется постоянным');}
    if(f.id==='room-settings'){const values={name:data.get('name'),stations:data.getAll('stations')};for(const key of ['impostors','tasks','cooldown','discussion','voting','emergencies'])values[key]=Number(data.get(key));if(values.stations.length<values.tasks)throw new Error('Выберите не меньше '+values.tasks+' станций или уменьшите число заданий');await roomAction('configure',values);setupStep=2;renderRoom();toast('Задания сохранены');}
    if(f.id==='chat-form')await sendChat(data.get('text'));
    if(f.id==='server-settings'){const values=Object.fromEntries(data);values.admin_ids=values.admin_ids.split(/[,\s]+/).filter(Boolean);await api('settings',values);toast('Сохранено. Перезапустите START.cmd');await settingsPage();}
  }catch(err){toast(err.message,true);}finally{if(button)button.disabled=false;}
});
document.addEventListener('input',e=>{
  if(e.target.closest('#room-settings')){dirty=true;const n=$('#unsaved');if(n)n.textContent='Есть несохранённые изменения';}
});
function updateTimers(){
  const stamp=room?.paused_at??now();syncSabotage();
  document.querySelectorAll('[data-deadline]').forEach(n=>{const left=Math.max(0,Math.ceil(+n.dataset.deadline-stamp));n.textContent=String(Math.floor(left/60)).padStart(2,'0')+':'+String(left%60).padStart(2,'0');});
  document.querySelectorAll('[data-cooldown]').forEach(n=>{const left=Math.max(0,Math.ceil(+n.dataset.cooldown-stamp));n.textContent=left?String(Math.floor(left/60)).padStart(2,'0')+':'+String(left%60).padStart(2,'0'):'ГОТОВО';n.closest('.kill-meter')?.classList.toggle('ready',!left);});
  if(room?.me?.role==='impostor'){
    const left=Math.max(0,Math.ceil((room.sabotage_ready||0)-stamp)),blocked=sabotageBusy||room.phase!=='running'||room.paused_at!=null||!!room.sabotage||left>0;
    document.querySelectorAll('[data-action="launch-sabotage"]').forEach(b=>b.disabled=blocked);
    const counter=$('[data-sabotage-countdown]'),hint=$('[data-sabotage-hint]');if(counter)counter.textContent=room.sabotage?'АКТИВЕН':left?String(Math.floor(left/60)).padStart(2,'0')+':'+String(left%60).padStart(2,'0'):'ГОТОВО';if(hint)hint.textContent=room.paused_at!=null?'Игра на паузе':room.sabotage?'Дождитесь устранения текущего саботажа':left?'Общая перезарядка после устранения — 90 секунд':'Нажмите кнопку · запуск из любого места';
  }
  if(room?.me?.status==='dead'){
    const left=Math.max(0,Math.ceil((room.me.ghost_ready||0)-stamp)),active=room.phase==='running'&&room.paused_at==null;
    document.querySelectorAll('[data-ghost-row]').forEach(row=>{row.classList.toggle('waiting',left>0);const b=row.querySelector('[data-ghost-button]');const task=room.me.tasks.find(t=>t.station===b?.dataset.station);if(b)b.disabled=!active||left>0||taskBlocked(task)||(task?.ready_at||0)>stamp;const timer=row.querySelector('[data-ghost-countdown]');if(timer)timer.textContent=left?'Следующее задание через '+String(Math.floor(left/60)).padStart(2,'0')+':'+String(left%60).padStart(2,'0'):'';});
  }
}
async function refreshRoom(force=false){
  if(!room)return;const updated=await api('rooms/'+room.code);serverOffset=updated.time-Date.now()/1000;
  const sig=JSON.stringify({...updated,time:0});room=updated;
  if(scannedObject&&(room.phase!=='running'||room.paused_at!=null)){scannedObject=null;closeModal();}
  if(currentPuzzle&&(room.phase!=='running'||room.paused_at!=null||currentPuzzle.epoch!==room.epoch||room.sabotage?.blocked.includes(currentPuzzle.station.kind))){closeModal();toast('Игра изменилась. Текущий незавершённый этап сброшен');}
  if(force||sig!==roomSignature&&!dirty&&!$('#room-settings')?.contains(document.activeElement)){roomSignature=sig;renderRoom();}
  if(currentRepair){if(room.phase!=='running'||room.paused_at!=null||room.me?.status!=='alive'||room.sabotage?.id!==currentRepair.id){closeModal();}else currentRepair.controller?.update(room.sabotage);}
  updateTimers();
}
async function poll(){
  if(pollBusy||!token||document.hidden)return;pollBusy=true;
  try{if(page==='room')await refreshRoom();else if(page==='home'&&!$('#modal').open){const old=JSON.stringify(me?.rooms);await loadMe();if(JSON.stringify(me.rooms)!==old)renderHome();}$('#connection').textContent=info.mode==='demo'?'Деморежим':'Сервер подключён';}
  catch(e){$('#connection').textContent='Связь потеряна';if(!token)loginScreen();}finally{pollBusy=false;}
}
async function boot(){
  try{
    info=await api('info');kindNames=info.kinds||kindNames;
    const key=new URLSearchParams(location.hash.slice(1)).get('setup');
    if(key){history.replaceState(null,'',location.pathname+location.search);const r=await api('auth/local',{key});setToken(r.token);sessionStorage.setItem('aul_owner',token);}
    else if(tg()?.initData&&!sessionStorage.getItem('aul_test_host')){tg().ready();tg().expand();setToken((await api('auth/telegram',{initData:tg().initData})).token);}
    if(token){try{await home();await consumeStart();}catch(e){if(!token)loginScreen();toast(e.message,true);}}
    else loginScreen();
  }catch(e){$('#main').innerHTML=`<div class="card"><h2>Не удалось подключиться</h2><p class="muted">${esc(e.message)}</p><p class="small">Проверьте окно запуска сервера и обновите страницу.</p></div>`;}
  setInterval(poll,2500);setInterval(updateTimers,250);
}
boot();
