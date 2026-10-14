(function () {
  'use strict';
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const date = v => v ? new Date(v).toLocaleString(undefined, {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : '';
  const labels = {bot:'With the bot', needs_human:'Waiting for a person', human:'With the team'};
  let workspace, view = '', offset = 0, total = 0, selected = null, inboxRows = [], debounce;
  async function api(path, method='GET', body) {
    const response = await fetch(path, {method, headers:{'Content-Type':'application/json','X-CSRF-Token':$('meta[name="csrf-token"]').content},body:body === undefined ? undefined : JSON.stringify(body)});
    const data = await response.json();
    if (response.status === 401) { location.href='/login?next='+encodeURIComponent(location.pathname+location.search); throw Error('Sign in to continue'); }
    if (!response.ok) throw Error(data.error || 'The request could not be completed');
    return data;
  }
  function error(e) { $('#workspace-error').hidden=false; $('#workspace-error').textContent=e.message; }
  function clearError() { $('#workspace-error').hidden=true; }
  function renderBots() {
    const q=$('#bot-search').value.toLowerCase();
    const bots=workspace.bots.filter(b=>b.name.toLowerCase().includes(q));
    $('#bot-directory').innerHTML=bots.map(b=>{
      const r=b.readiness, next=r.checks.find(c=>!c.done);
      return '<article class="bot-entry"><div class="bot-avatar" aria-hidden="true">'+esc(b.name.slice(0,1))+'</div><div><h3><a href="/bots/'+b.id+'#overview">'+esc(b.name)+'</a></h3><p>'+ (b.mode==='internal_kb'?'Private knowledge base':'Website assistant')+' / '+r.sources+(r.sources===1?' source':' sources')+(b.waiting?' / '+b.waiting+' waiting':'')+'</p></div><div class="bot-ready"><span class="'+(r.configured?'ready-label':'attention-label')+'">'+(r.configured?'Setup complete':r.completed+' of '+r.total+' setup steps')+'</span><progress max="'+r.total+'" value="'+r.completed+'" aria-label="Setup progress"></progress><span>'+esc(r.source_errors?'Training needs attention':next ? next.label : 'Test answers before sharing')+'</span></div><a class="btn btn-small" href="/bots/'+b.id+'#overview">Open bot</a></article>';
    }).join('') || '<div class="empty-state"><h2>'+(q?'No matching bots':'Create your first bot')+'</h2><p>'+(q?'Try another name.':'Use Create bot to start with a template or a blank bot.')+'</p></div>';
  }
  async function loadWorkspace() {
    workspace=await api('/api/workspace');
    const s=workspace.stats;
    $('#workspace-summary').innerHTML=[['Conversations',s.conversations],['Waiting for a person',s.waiting],['Leads captured',s.leads],['Qualified leads',s.qualified]].map(([label,n],i)=>'<div class="summary-item '+(i===1 && n?'attention':'')+'"><strong>'+n+'</strong><span>'+label+'</span></div>').join('');
    $('#waiting-count').textContent=s.waiting || ''; $('#failed-count').textContent=s.failed_deliveries || '';
    const old=$('#inbox-bot').value;
    $('#inbox-bot').innerHTML='<option value="">All bots</option>'+workspace.bots.map(b=>'<option value="'+b.id+'">'+esc(b.name)+'</option>').join('');
    $('#inbox-bot').value=old;
    renderBots();
  }
  async function changeView(name) {
    view=['overview','inbox','delivery'].includes(name)?name:'overview'; clearError();
    document.querySelectorAll('[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===view);b.setAttribute('aria-current', b.dataset.view===view?'page':'false');});
    document.querySelectorAll('[data-view-panel]').forEach(p=>p.hidden=p.dataset.viewPanel!==view);
    const names={overview:['Studio overview','Your bots and visitor conversations, in one place.'],inbox:['Shared inbox','Reply to visitors across every bot.'],delivery:['Notification delivery','See what arrived and what needs another attempt.']};
    $('#view-title').textContent=names[view][0]; $('#view-description').textContent=names[view][1];
    history.replaceState(null,'','/?view='+view);
    try { if(view==='inbox') await loadInbox(); if(view==='delivery') await loadDeliveries(); } catch(e) { error(e); }
  }
  async function loadInbox() {
    const query=new URLSearchParams({status:$('#inbox-status').value,bot_id:$('#inbox-bot').value,q:$('#inbox-search').value,offset});
    const data=await api('/api/inbox?'+query);
    inboxRows=data.conversations; total=data.total;
    $('#inbox-list').innerHTML=inboxRows.map(c=>'<button class="conversation-row '+(selected && selected.id===c.id?'selected':'')+'" data-conversation="'+c.id+'"><strong>'+esc(c.bot_name)+'</strong>'+(c.unread?' <span class="badge badge-warn">'+c.unread+' unread</span>':'')+'<p>'+esc(c.preview || 'No messages yet')+'</p><div class="conversation-meta"><span>'+esc(c.resolved?'Resolved':labels[c.status])+'</span><time>'+date(c.last_message_at)+'</time></div>'+(c.assignee?'<small>Assigned to '+esc(c.assignee)+'</small>':'')+'</button>').join('') || '<div class="empty-state"><h2>No conversations here</h2><p>Try another filter or check back after a visitor starts a chat.</p></div>';
    $('#inbox-page').textContent=total ? (offset+1)+' to '+Math.min(offset+40,total)+' of '+total : 'No conversations';
    $('#inbox-prev').disabled=offset===0; $('#inbox-next').disabled=offset+40>=total;
  }
  async function openConversation(c) {
    selected=c;
    const data=await api('/api/bots/'+c.bot_id+'/conversations/'+c.id);
    if (!selected || selected.id!==c.id) return;
    const convo=data.conversation;
    $('#inbox-detail').innerHTML='<div class="thread-heading"><h2>'+esc(c.bot_name)+'</h2><div class="small muted">Conversation #'+c.id+' / '+esc(labels[convo.status])+'</div><div class="small muted">'+esc(convo.page_url)+'</div><div class="thread-controls"><label><span class="sr-only">Assigned to</span><input id="thread-assignee" type="text" placeholder="Assign to a name" maxlength="80" value="'+esc(convo.assignee)+'"></label><button class="btn btn-small" id="assign-thread">Assign</button><button class="btn btn-small" id="resolve-thread">'+(convo.resolved?'Reopen':'Resolve')+'</button><button class="btn btn-small" id="return-to-bot">Return to bot</button></div></div><div class="thread-messages">'+data.messages.map(m=>'<div class="thread-message '+esc(m.role)+'"><strong>'+esc(m.role==='user'?'Visitor':m.role==='agent'?'Team':'Bot')+' / '+date(m.created_at)+'</strong><p>'+esc(m.content)+'</p></div>').join('')+'</div><form class="reply-form" id="reply-form"><label class="field"><span>Reply to the visitor</span><textarea id="thread-reply" rows="3" maxlength="4000" required placeholder="Write a reply..."></textarea></label><div class="small muted">Sending a reply pauses the bot for this conversation.</div><div class="modal-actions"><button class="btn btn-primary">Send reply</button></div></form>';
    const msgs=$('.thread-messages'); msgs.scrollTop=msgs.scrollHeight;
    await api('/api/inbox/'+c.id,'PATCH',{read:true,read_up_to:data.messages.length?data.messages[data.messages.length-1].id:0});
    $('#assign-thread').onclick=async()=>{try{await api('/api/inbox/'+c.id,'PATCH',{assignee:$('#thread-assignee').value}); await loadInbox();}catch(e){error(e);}};
    $('#resolve-thread').onclick=async()=>{try{await api('/api/inbox/'+c.id,'PATCH',{resolved:!convo.resolved});await openConversation(c);await loadInbox();}catch(e){error(e);}};
    $('#return-to-bot').onclick=async()=>{try{await api('/api/bots/'+c.bot_id+'/conversations/'+c.id+'/status','POST',{status:'bot'});await openConversation(c);await loadInbox();}catch(e){error(e);}};
    $('#reply-form').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;try{await api('/api/bots/'+c.bot_id+'/conversations/'+c.id+'/reply','POST',{message:$('#thread-reply').value});await openConversation(c);await loadInbox();}catch(err){error(err);button.disabled=false;}};
    await loadInbox();
    await loadWorkspace();
  }
  async function loadDeliveries() {
    const data=await api('/api/deliveries');
    $('#delivery-list').innerHTML=data.deliveries.map(d=>'<div class="delivery-row"><div><strong>'+esc(d.bot_name || 'Platform')+'</strong><small>'+esc(d.channel==='email'?'Email':'Connected app')+' / '+date(d.created_at)+' / '+d.attempts+' attempts</small>'+(d.error?'<small class="error-text">'+esc(d.error)+'</small>':'')+'</div><span class="badge '+(d.status==='delivered'?'badge-ok':d.status==='failed'?'badge-err':'badge-warn')+'">'+esc(d.status)+'</span>'+(d.status==='failed'?'<button class="btn btn-small" data-retry="'+d.id+'">Retry delivery</button>':'')+'</div>').join('') || '<div class="empty-state"><h2>No deliveries yet</h2><p>Connect notifications in a bot’s Lead capture settings. Delivery history will appear here.</p></div>';
  }
  document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>changeView(b.dataset.view));
  $('#bot-search').oninput=renderBots;
  $('#inbox-list').onclick=e=>{const b=e.target.closest('[data-conversation]');if(b)openConversation(inboxRows.find(c=>c.id===Number(b.dataset.conversation))).catch(error);};
  $('#delivery-list').onclick=async e=>{const b=e.target.closest('[data-retry]');if(!b)return;b.disabled=true;try{await api('/api/deliveries/'+b.dataset.retry+'/retry','POST',{});await loadDeliveries();}catch(err){error(err);b.disabled=false;}};
  ['inbox-status','inbox-bot'].forEach(id=>$('#'+id).onchange=()=>{offset=0;loadInbox().catch(error);});
  $('#inbox-search').oninput=()=>{clearTimeout(debounce);debounce=setTimeout(()=>{offset=0;loadInbox().catch(error);},250);};
  $('#refresh-inbox').onclick=async()=>{try{if(selected)await openConversation(selected);await loadInbox();}catch(e){error(e);}};
  $('#inbox-prev').onclick=()=>{offset=Math.max(0,offset-40);loadInbox().catch(error);};
  $('#inbox-next').onclick=()=>{offset+=40;loadInbox().catch(error);};
  const dialog=$('#create-dialog');
  $('#new-bot-btn').onclick=()=>dialog.showModal();
  dialog.querySelectorAll('[data-close-dialog]').forEach(b=>b.onclick=()=>dialog.close());
  let createdBot=null;
  $('#create-form').onsubmit=async e=>{
    e.preventDefault();$('#create-submit').disabled=true;$('#create-error').textContent='';
    try{
      if(!createdBot) createdBot=(await api('/api/bots','POST',{name:$('#new-name').value,preset:$('#new-preset').value})).bot;
      const url=$('#new-url').value.trim();
      if(url) await api('/api/bots/'+createdBot.id+'/jobs','POST',{type:'url',url,max_pages:5,recrawl_hours:168});
      location.href='/bots/'+createdBot.id+'#overview';
    }catch(err){$('#create-error').textContent=err.message+(createdBot?' Your bot was created. Clear the website field to continue without importing.':'');$('#create-submit').disabled=false;}
  };
  api('/api/presets').then(d=>d.presets.forEach(p=>{const opt=document.createElement('option');opt.value=p.slug;opt.textContent=p.name;$('#new-preset').appendChild(opt);})).catch(error);
  loadWorkspace().then(()=>changeView(new URLSearchParams(location.search).get('view') || 'overview')).catch(error);
  async function refreshTranscript() {
    if (!selected || !$('.thread-messages')) return;
    const current=selected;
    const data=await api('/api/bots/'+current.bot_id+'/conversations/'+current.id);
    if (!selected || selected.id!==current.id) return;
    const panel=$('.thread-messages');
    const nearBottom=panel.scrollHeight-panel.scrollTop-panel.clientHeight<80;
    const html=data.messages.map(m=>'<div class="thread-message '+esc(m.role)+'"><strong>'+esc(m.role==='user'?'Visitor':m.role==='agent'?'Team':'Bot')+' / '+date(m.created_at)+'</strong><p>'+esc(m.content)+'</p></div>').join('');
    if(panel.innerHTML!==html) {panel.innerHTML=html;if(nearBottom){panel.scrollTop=panel.scrollHeight;await api('/api/inbox/'+current.id,'PATCH',{read:true,read_up_to:data.messages.length?data.messages[data.messages.length-1].id:0});}}
  }
  setInterval(()=>{if(document.hidden)return;if(view==='inbox'){loadInbox().catch(error);refreshTranscript().catch(error);}else if(view==='delivery')loadDeliveries().catch(error);},10000);
})();
