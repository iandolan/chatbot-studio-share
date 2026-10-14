(function () {
  'use strict';
  const root = document.querySelector('#behavior-profile');
  if (!root) return;
  const id = location.pathname.split('/').pop();
  const field = name => root.querySelector('[name="'+name+'"]');
  const status = root.querySelector('[role="status"]');
  let templates = [], dirty = false;
  async function api(path, method='GET', body) {
    const response = await fetch(path, {method, headers:{'Content-Type':'application/json', 'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content}, body:body === undefined ? undefined : JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || 'Request failed');
    return data;
  }
  function fill(p) {
    ['template','goal','response_length','lead_capture','handoff_message'].forEach(k=>field(k).value=p[k]);
    ['questions','boundaries'].forEach(k=>field(k).value=p[k].join('\n'));
    field('enabled').checked=p.enabled;
    root.querySelectorAll('[name="action"]').forEach(el=>el.checked=p.allowed_actions.includes(el.value));
  }
  function read() {
    const p={enabled:field('enabled').checked};
    ['template','goal','response_length','lead_capture','handoff_message'].forEach(k=>p[k]=field(k).value);
    ['questions','boundaries'].forEach(k=>p[k]=field(k).value.split('\n').map(x=>x.trim()).filter(Boolean));
    p.allowed_actions=Array.from(root.querySelectorAll('[name="action"]:checked')).map(el=>el.value);
    return p;
  }
  root.addEventListener('input',()=>{dirty=true;status.textContent='Unsaved behavior changes';});
  window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
  root.querySelector('#use-behavior-template').onclick=()=>{
    const chosen=templates.find(t=>t.id===field('template').value);
    fill(chosen.config);dirty=true;status.textContent='Template loaded. Review and save to apply.';
  };
  root.querySelector('#save-behavior-profile').onclick=async function () {
    this.disabled=true;
    try {await api('/api/bots/'+id,'PATCH',{behavior_config:read()});dirty=false;status.textContent='Behavior saved. New messages use these settings.';}
    catch(e){status.textContent=e.message;}finally{this.disabled=false;}
  };
  root.querySelector('#preview-behavior-profile').onclick=async function () {
    this.disabled=true;
    try {
      const p=await api('/api/bots/'+id+'/behavior-preview','POST',{behavior_config:read()});
      const box=root.querySelector('#behavior-result');box.hidden=false;box.replaceChildren();
      const lines=[p.enabled?'Profile enabled':'Profile disabled: existing bot settings apply', 'Available actions: '+(p.actions.join(', ')||'Answer only'),...p.warnings,'Try these questions in the test chat:',...p.scenarios,p.note];
      lines.forEach(text=>{const el=document.createElement('p');el.textContent=text;box.appendChild(el);});
      status.textContent='Preview updated. Unsaved settings have not been applied.';
    }catch(e){status.textContent=e.message;}finally{this.disabled=false;}
  };
  Promise.all([api('/api/behavior-templates'),api('/api/bots/'+id)]).then(([catalog,data])=>{
    templates=catalog.templates;
    templates.forEach(t=>{const option=document.createElement('option');option.value=t.id;option.textContent=t.label;field('template').appendChild(option);});
    fill(data.bot.behavior_config);root.querySelectorAll('button').forEach(b=>b.disabled=false);
  }).catch(e=>{status.textContent=e.message;});
}());
