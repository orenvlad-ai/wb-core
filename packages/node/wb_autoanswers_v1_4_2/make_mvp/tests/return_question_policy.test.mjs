import assert from "node:assert/strict";
import test from "node:test";
import {applyReturnQuestionPolicy, isGenericReturnQuestion, returnQuestionReplacement, RETURN_QUESTION_POLICY} from "../scripts/return_question_policy.mjs";
import {runDraftGuard} from "../scripts/draft_guard.mjs";
import {runJob} from "../scripts/orchestrator.mjs";
import {MemoryStore} from "../scripts/memory_store.mjs";
import {createFixtureRunner, loadScenarios} from "./fixture_runtime.mjs";
const scenarios = await loadScenarios();
for (const text of ["Как можно оформить возврат?", "Можно ли оформить возврат?", "Здравствуйте, как вернуть деньги?", "Хочу вернуть товар. Подскажите, как?", "Подскажите, пожалуйста, как мне оформить возврат товара?", "Возврат как оформить?", "Как сделать возврат?", "Хочу оформить заявку на возврат", "Как мне вернуть деньги?"]) test(`generic ${text}`, async () => {
 const s = structuredClone(scenarios.wb_support); s.raw.text=text; s.raw.review_id="generic-return";
 s.drafts=["Здравствуйте. Напишите, пожалуйста, в чат с продавцом — мы постараемся разобраться в вашей ситуации. Код обращения: {{case_code}}."];
 const store=new MemoryStore(); const r=await runJob(s.raw,{roleRunner:createFixtureRunner(s),store});
 assert.equal(r.result.route,"seller_chat"); assert.equal(r.result.outcome,"ready"); assert.match(r.result.route_reason,new RegExp(RETURN_QUESTION_POLICY));
 assert.match(r.result.case_code,/^[А-ЯЁ][0-9]{4}$/u); assert.equal(r.result.final_reply.split(r.result.case_code).length,2); assert.equal((await store.listCaseCodes()).length,1);
});
test("whole content preserves factual status, defects, and media",async()=>{
 for(const field of ["text","pros","cons"]) for(const fact of ["Возврат одобрен, деньги не пришли","Пришло разбитое стекло","Пришла другая модель"]){const r={text:"Как вернуть деньги?",pros:"",cons:""};r[field]+=` ${fact}`;assert.equal(isGenericReturnQuestion(r),false);}
 for(const text of ["Когда придут деньги по оформленному возврату?","Когда придут деньги?","Когда вернут деньги?"])assert.equal(isGenericReturnQuestion({text}),false);
 assert.equal(isGenericReturnQuestion({text:"Как вернуть товар?"},{photos:[{}]}),false);
 for(const name of ["wb_support","wb_return"]){const s=structuredClone(scenarios[name]);s.raw.text=`Как вернуть деньги? ${s.raw.text}`;const r=await runJob(s.raw,{roleRunner:createFixtureRunner(s),store:new MemoryStore()});assert.equal(r.result.route,name);}
 const c={issues:[{code:"DEFECT_OUT_OF_BOX"}],risk_flags:[],media_status:"none"};assert.equal(applyReturnQuestionPolicy(c,{review:{text:"Как вернуть деньги?"},media:{}}),c);
});
test("canonical allocation probes occupied code and reuses reservation",()=>{
 const p={review:{review_id:"synthetic",review_version:"1",text:"Как вернуть деньги?"},processingKey:"synthetic|1|1.4.2"};
 const a=returnQuestionReplacement(p);const b=returnQuestionReplacement({...p,existing:[{idempotency_key:"other",case_code:a.case_code}]});assert.notEqual(a.case_code,b.case_code);
 assert.equal(returnQuestionReplacement({...p,existing:[{idempotency_key:p.processingKey,case_code:b.case_code}]}).case_code,b.case_code);
});
function guard(route,text,code=null){return runDraftGuard({route,case_code:code,applied_cta:route,draft_reply:text,requested_materials:[]},{final_route:route,case_code:code});}
test("noun context passes but real dual CTA rejects",()=>{
 for(const text of ["Здравствуйте. Оформление возврата регулируется Wildberries. Обратитесь в поддержку Wildberries.","Здравствуйте. Статус заявки на возврат проверяется в поддержке Wildberries."])assert.deepEqual(guard("wb_support",text),[]);
 for(const action of ["Оформите возврат","Оформить возврат можно в покупках","Подайте заявку на возврат","Возврат оформляйте через WB","Заполните заявку на возврат","Направьте заявку на возврат","Отправляйте заявку на возврат","Заявку на возврат подавайте через WB"])assert.ok(guard("wb_support",`Здравствуйте. ${action}. Обратитесь в поддержку Wildberries.`).length);
 assert.ok(guard("seller_chat","Здравствуйте. Напишите в чат с продавцом по коду А1234. Оформите возврат.","А1234").includes("MULTIPLE_ROUTES"));
});
test("promise, public media, extra CTA and missing/doubled code still fail",()=>{
 for(const extra of [" Мы вернём деньги."," Пришлите фото."," Обратитесь в поддержку Wildberries."," Код А1234."])assert.ok(guard("seller_chat",`Здравствуйте. Напишите в чат с продавцом по коду А1234.${extra}`,"А1234").length);
 assert.ok(guard("seller_chat","Здравствуйте. Напишите в чат с продавцом.","А1234").includes("CASE_CODE_INVALID"));
});
