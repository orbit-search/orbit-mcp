/** MCP boundary regressions: no polling loops, stable recovery IDs, and isolated person results. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { createOrbitServer } from '../build/server.js';
const search = { search_id: 's1', request_id: 'r1', status: 'running', candidate_discovery: true, profile_depth: 'full', include_profile: true,
  results: [], created_at: '2026-11-01T00:00:00Z', updated_at: '2026-11-01T00:00:00Z', links: { status: '/v3/search/s1' },
  progress: { phase: 'discovering', completed_results: 0, observed_results: 0, percent: null, eta_seconds: null, poll_after_seconds: 3 } };
async function withClient(respond, run) {
  const previous = globalThis.fetch; const calls = [];
  globalThis.fetch = async (url, init = {}) => { const request = { url: String(url), ...init, body: init.body ? JSON.parse(init.body) : undefined }; calls.push(request); return respond(request, calls.length); };
  const server = createOrbitServer('sk_orb_fixture'); const client = new Client({ name: 'async-search-test', version: '1' });
  const [a,b] = InMemoryTransport.createLinkedPair();
  try { await Promise.all([server.connect(b), client.connect(a)]); await client.listTools(); await run(client, calls); }
  finally { globalThis.fetch = previous; await client.close(); await server.close(); }
}
const response = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
test('discovery returns running after one POST and preserves depth/candidate defaults', async () => {
  await withClient(() => response(search, 202), async (client, calls) => {
    for (const args of [{ query: 'Ada', candidate_discovery: true, profile_depth: 'full', request_id: 'r1' }, { query: 'Ada', candidate_discovery: true, request_id: 'r2' }]) {
      const before = calls.length; const result = await client.callTool({ name: 'search_people', arguments: args });
      assert.notEqual(result.isError, true); assert.equal(result.structuredContent.status, 'running'); assert.equal(calls.length, before + 1);
      assert.equal(calls.at(-1).headers.Prefer, 'respond-async'); assert.equal(calls.at(-1).body.profile_depth, args.profile_depth ?? 'partial');
      assert.equal(calls.at(-1).body.limit, 10); assert.equal(calls.at(-1).body.candidate_discovery_limit, undefined);
      assert.deepEqual(result.structuredContent.progress, search.progress);
    }
  });
});
test('lost responses retain the same request and recovery never starts another search', async () => {
  await withClient((_r,n) => n === 1 ? response({},504) : response(search,202), async (client,calls) => {
    const args = { query: 'Ada', profile_depth: 'full', request_id: 'stable' };
    const failed = await client.callTool({ name:'search_people', arguments:args });
    assert.equal(failed.isError,true); assert.match(failed.content[0].text,/request_id=stable/); assert.match(failed.content[0].text,/may still be running/);
    const retry = await client.callTool({name:'search_people',arguments:args}); assert.notEqual(retry.isError,true);
    assert.deepEqual(calls[0].body,calls[1].body); assert.equal(calls.length,2);
    await client.callTool({name:'get_search_status',arguments:{search_id:'s1'}}); assert.equal(calls[2].method ?? 'GET','GET');
  });
});
test('find explicitly selects quick and forwards the name required for URL discovery', async () => {
  await withClient(() => response({...search,profile_depth:'quick'},202), async (client,calls) => {
    await client.callTool({name:'find_people',arguments:{signals:{linkedin_url:'https://linkedin.com/in/ada'},intent:{names:['Ada']},candidate_discovery:true,request_id:'find'}});
    assert.equal(calls[0].body.profile_depth,'quick'); assert.deepEqual(calls[0].body.intent,{names:['Ada']});
    const invalid = await client.callTool({name:'search_people',arguments:{query:'Ada',mode:'sync'}}); assert.equal(invalid.isError,true); assert.equal(calls.length,1);
  });
});
test('batch polling retains successful people and failures without replacing the job', async () => {
  const job = '11111111-1111-4111-8111-111111111111'; const page={job_id:job,status:'running',items:[{id:'oscar',status:'completed',result:{...search,status:'completed',results:[{profile_id:'p1',status:'ready',generation_level:3}]}},{id:'joshua',status:'processing',result:null,failure:{code:'upstream',retryable:true}}],next_offset:2};
  await withClient(r=>response(r.method==='POST'?{job_id:job,status:'queued',request_id:'batch',counts:{total:2,pending:2},billing:{consumed_credits:0}}:page),async(client,calls)=>{
    const started=await client.callTool({name:'search_people_batch',arguments:{request_id:'batch',people:[{person_key:'oscar',query:'Oscar'},{person_key:'joshua',query:'Joshua'}]}});
    assert.equal(started.structuredContent.job_id,job); assert.equal(calls.length,1); assert.equal(calls[0].body.items[0].id,'oscar');
    const poll=await client.callTool({name:'get_people_batch_status',arguments:{job_id:job}}); assert.deepEqual(poll.structuredContent,page); assert.notEqual(poll.isError,true);
  });
});
test('enrich returns running and exposes a separate status tool', async()=>{
  const enrichment={request_id:'e1',profile_id:'p1',status:'running',operation:'full',include_profile:true};
  await withClient(()=>response(enrichment,202),async(client,calls)=>{
    const result=await client.callTool({name:'enrich_profile',arguments:{profile_id:'p1',operation:'full',request_id:'e1'}});
    assert.notEqual(result.isError,true); assert.equal(result.structuredContent.status,'running'); assert.equal(calls.length,1);
    await client.callTool({name:'get_enrichment_status',arguments:{request_id:'e1'}}); assert.match(calls[1].url,/enrich\/requests\/e1$/);
  });
});
