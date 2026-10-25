/* Execute actual frontend helpers in a minimal DOM, without network or packages. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
class Element {
  constructor() { this.children=[]; this.style={}; this.value=''; this.scrollHeight=0; this.scrollTop=0; this.clientHeight=0; this.classList={add(){},remove(){}}; }
  get firstChild() { return this.children[0]; }
  appendChild(child) { this.children.push(child); child.parentNode=this; return child; }
  removeChild(child) { this.children=this.children.filter(x=>x!==child); }
  setAttribute() {}
  focus() {}
}
async function main() {
  const elements={}, saved=new Map(), calls=[];
  const window={localStorage:{getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},
    matchMedia:()=>({matches:true}),setTimeout:(fn,ms)=>setTimeout(fn,ms===1000?0:ms),clearTimeout,
    fetch:async()=>{throw Error('unconfigured fetch');}};
  const document={getElementById:id=>elements[id]||(elements[id]=new Element()),createElement:()=>new Element(),
    createTextNode:text=>({textContent:text}),readyState:'loading'};
  let source=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
  source=source.slice(0,source.lastIndexOf("  if (document.readyState === 'loading')"))+
    "window.test={recoverableChat,syncSession,setComposerEnabled,getHistory:()=>history,getPendingKey:()=>KEY_PENDING};})();";
  vm.runInNewContext(source,{window,document,navigator:{},Promise,Date,Math,Uint8Array,JSON,encodeURIComponent});
  const api=window.test;
  api.setComposerEnabled(false);
  assert.equal(elements.input.disabled,false,'waiting should leave the draft editable');
  assert.equal(elements.send.disabled,true,'avoid concurrent sends');
  let stage=0;
  window.fetch=async(url,opts)=>{
    calls.push({url,body:opts.body});
    const value=stage++===0?{status:'running'}:{status:'completed',reply:'我在'};
    return {ok:true,status:value.status==='completed'?200:202,text:async()=>JSON.stringify(value)};
  };
  const result=await api.recoverableChat({request_id:'a'.repeat(32),text:'在吗'});
  assert.equal(result.reply,'我在');
  assert.equal(calls.filter(x=>x.body).length,1,'poll instead of posting a duplicate');
  assert.ok(calls[1].url.includes('request_id='+'a'.repeat(32)));
  calls.length=0;stage=0;
  window.fetch=async(url,opts)=>{
    calls.push({url,body:opts.body});
    if(stage++===0)throw Error('connection closed after server accepted');
    return {ok:true,status:200,text:async()=>JSON.stringify({status:'completed',reply:'已经答过'})};
  };
  assert.equal((await api.recoverableChat({request_id:'b'.repeat(32),text:'同一条'})).reply,'已经答过');
  assert.equal(calls.filter(x=>x.body).length,1,'ambiguous network failure queries the same request');
  saved.set('ella.history',JSON.stringify([{role:'me',text:'搭档的私事',ts:1}]));
  window.fetch=async()=>({ok:true,status:200,text:async()=>JSON.stringify({view_id:'owner-view',role:'owner',known:true})});
  await api.syncSession();
  assert.equal(api.getHistory()[0].text,'搭档的私事','owner keeps legacy local history');
  saved.set(api.getPendingKey(),JSON.stringify({request_id:'old-owner'}));
  window.fetch=async()=>({ok:true,status:200,text:async()=>JSON.stringify({view_id:'friend-view',role:'friend',known:true})});
  await api.syncSession();
  assert.equal(api.getHistory().length,0,'visitor cannot inherit owner local history');
  assert.equal(saved.get(api.getPendingKey()),undefined,'visitor cannot replay owner pending request');
  window.fetch=async()=>({ok:true,status:200,text:async()=>JSON.stringify({reply:'旧版接口回复'})});
  assert.equal((await api.recoverableChat({request_id:'c'.repeat(32),text:'兼容'})).reply,'旧版接口回复');
  let pushSource=fs.readFileSync(path.join(__dirname,'../web/push.js'),'utf8');
  pushSource=pushSource.slice(0,pushSource.lastIndexOf("  if (document.readyState === 'loading')"))+'window.pushTest=mergeOutreach;})();';
  let reloads=0;
  saved.delete('ella.pending.owner-view');
  let pushMessage={id:'notification-1',text:'主动问候',ts:'2026-10-25T12:00:00'};
  const pushFetch=async(url)=>({ok:true,json:async()=>url==='/api/session'?{role:'owner',view_id:'owner-view'}:pushMessage});
  vm.runInNewContext(pushSource,{window,document,localStorage:window.localStorage,fetch:pushFetch,
                                location:{reload:()=>reloads++},Uint8Array,JSON,console});
  await window.pushTest();
  assert.ok(JSON.parse(saved.get('ella.history.owner-view')).some(x=>x.text==='主动问候'));
  assert.ok(!JSON.parse(saved.get('ella.history')).some(x=>x.text==='主动问候'));
  assert.equal(reloads,1);
  elements.input.value='未发送的草稿';pushMessage={...pushMessage,id:'notification-2'};
  await window.pushTest();
  assert.equal(reloads,1,'push merge cannot reload away a draft');
  console.log('Frontend quality: 15 checks passed.');
}
main().catch(err=>{console.error(err);process.exitCode=1;});
