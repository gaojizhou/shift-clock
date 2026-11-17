import test from 'node:test';
import assert from 'node:assert/strict';
import { ReportRequestQueue } from '../src/reportRequestQueue.ts';
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };

test('serializes work and shares identical in-flight requests', async () => {
  const q = new ReportRequestQueue(), gate = deferred(), entered = deferred();
  const calls = [];
  const task = () => { calls.push('first'); entered.resolve(); return gate.promise; };
  const a = q.run({key:'a', label:'first', current:()=>true, task});
  const same = q.run({key:'a', label:'first', current:()=>true, task});
  const b = q.run({key:'b', label:'second', current:()=>true, task:async()=>{ calls.push('second'); return 2; }});
  await entered.promise;
  assert.equal(a, same); assert.deepEqual(calls, ['first']);
  gate.resolve(1);
  assert.deepEqual(await Promise.all([a,same,b]), [1,1,2]);
  assert.deepEqual(calls, ['first','second']);
});

test('retries only 429, announces waiting and keeps the next job queued', async () => {
  const q = new ReportRequestQueue(), phases = [], calls = [];
  q.subscribe(s=>phases.push(s.waiting));
  let count=0;
  const a=q.run({key:'a',label:'preview',current:()=>true,task:async()=>{
    calls.push('a'); if (++count===1) throw {status:429,retryAfter:0.1}; return 1;
  }});
  const b=q.run({key:'b',label:'next',current:()=>true,task:async()=>{calls.push('b');return 2;}});
  await Promise.all([a,b]); assert.deepEqual(calls,['a','a','b']); assert.ok(phases.includes(true));
  let attempts=0;
  await assert.rejects(q.run({key:'fail',label:'fail',current:()=>true,task:async()=>{
    attempts++; throw Object.assign(new Error('invalid'),{status:422});
  }}),/invalid/);
  assert.equal(attempts,1);
});

test('drops obsolete queued work without releasing an active request early', async () => {
  const q=new ReportRequestQueue(), gate=deferred(), entered=deferred(); let old=true; const calls=[];
  const a=q.run({key:'a',label:'old',current:()=>old,task:()=>{entered.resolve();return gate.promise;}});
  const aResult=assert.rejects(a,{name:'AbortError'});
  const b=q.run({key:'b',label:'old queued',current:()=>old,task:async()=>calls.push('old')});
  const bResult=assert.rejects(b,{name:'AbortError'});
  await entered.promise; old=false;
  const c=q.run({key:'c',label:'new',current:()=>true,task:async()=>calls.push('new')});
  await Promise.resolve(); assert.deepEqual(calls,[]);
  gate.resolve(); await Promise.all([aResult,bResult,c]); assert.deepEqual(calls,['new']);
});

test('bounded waiting expires and leaves queue usable', async()=>{
  const q=new ReportRequestQueue();
  await assert.rejects(q.run({key:'a',label:'busy',current:()=>true,maxWaitMs:0,
    task:async()=>{throw {status:429};}}),/等待计算超时/);
  assert.equal(await q.run({key:'b',label:'ready',current:()=>true,task:async()=>7}),7);
});
