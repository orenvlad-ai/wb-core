/* Policy source receipts. Hosts own forms; this component never resubmits. */
(function(){
  'use strict';
  const ID=/^oppolicy_[a-f0-9]{32}$/;
  const DOMAINS=['legacy_proxy','proxy_v4_tax','wb_incident_policy'];
  function create(config,container,kinds){
    const scope=String(config.operator_policy_actor_scope||config.user_config_key||'');
    const refs=new Map(),unknown=new Set(),known=new Map();let busy=false,generation=0;
    const key=kind=>'wbc.policy.operations.'+scope+'.'+kind;
    const lockKey='wbc.policy.operations.'+scope+':mutation';
    function idsFor(kind){
      try{
        const ids=JSON.parse(localStorage.getItem(key(kind))||'[]');
        if(!Array.isArray(ids)||ids.some(id=>!ID.test(id)))throw Error('invalid identities');
        return [...new Set(ids)];
      }catch(_){throw Error('Не удалось прочитать номера операций. Новая отправка остановлена.');}
    }
    function refresh(){
      // Read the durable registry again after acquiring the actor/account lock.
      // A URL supplies only a GET routing hint and never writes that registry.
      const fresh=new Map();
      for(const kind of DOMAINS)for(const id of idsFor(kind))fresh.set(id,kind);
      const query=new URL(location.href).searchParams,id=query.get('policy_operation_id'),kind=query.get('policy_kind');
      if(ID.test(id||'')&&kinds.includes(kind))fresh.set(id,kind);
      refs.clear();for(const [id,kind] of fresh)refs.set(id,kind);
      for(const id of unknown)if(!refs.has(id))unknown.delete(id);
      for(const id of refs.keys())if(!known.has(id))unknown.add(id);
    }
    function retain(id,kind){
      const ids=idsFor(kind);if(!ids.includes(id))ids.push(id);
      localStorage.setItem(key(kind),JSON.stringify(ids));
      refs.set(id,kind);unknown.add(id);
    }
    function removeOwnRejected(id,kind){
      // An explicit negative receipt retires only this submitted identity.
      localStorage.setItem(key(kind),JSON.stringify(idsFor(kind).filter(value=>value!==id)));
      refs.delete(id);unknown.delete(id);
    }
    function locked(callback){
      if(!navigator.locks||!navigator.locks.request)throw Error('Браузер не может безопасно согласовать отправку между вкладками. Новая отправка остановлена.');
      return navigator.locks.request(lockKey,{mode:'exclusive'},callback);
    }
    function show(receipt){container.hidden=false;OperatorAcceptance.renderReceipt(container,receipt,{onClose:()=>{container.hidden=true;}});}
    function showUnknown(){container.hidden=false;OperatorAcceptance.renderState(container,null);const button=document.createElement('button');button.type='button';button.textContent='Проверить сохранение';button.addEventListener('click',restore);container.appendChild(button);}
    async function read(id,kind,captured){
      const before=known.get(id);
      const result=await OperatorAcceptance.readSameOperation({operation_id:id,domain:kind},async()=>{
        const response=await fetch(config.policy_operations_path+kind+'/'+id,{headers:{Accept:'application/json'}});
        if(!response.ok)return null;return response.json();
      });
      if(result.status==='accepted'){known.set(id,result.operation);unknown.delete(id);if(captured===generation)show(result.operation);return result.operation;}
      if(known.has(id)&&known.get(id)!==before)return known.get(id);
      unknown.add(id);if(captured===generation)showUnknown();return null;
    }
    async function restore(){
      try{
        refresh();const captured=++generation;
        for(const [id,kind] of refs){await read(id,kind,captured);if(captured!==generation)return;}
        if(unknown.size)showUnknown();
      }catch(_){showUnknown();}
    }
    async function submit(kind,url,payload){
      if(!kinds.includes(kind))throw Error('Недоступный вид операции.');
      if(busy)throw Error('Проверяем сохранение. Повторно отправлять не нужно.');
      busy=true;
      try{
        // Capture the clicked operands before the first lock/GET await.
        const capturedPayload=JSON.parse(JSON.stringify(payload));
        return await locked(async()=>{
          refresh();const captured=++generation;
          for(const id of [...unknown])await read(id,refs.get(id),captured);
          if(unknown.size){showUnknown();throw Error('Результат прошлой операции неизвестен. Проверьте её сохранение.');}
          container.hidden=false;OperatorAcceptance.renderState(container,null);
          const id='oppolicy_'+crypto.randomUUID().replace(/-/g,'');retain(id,kind);
          let response,result;
          try{response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json',Accept:'application/json','X-Operator-Request-ID':id},body:JSON.stringify({...capturedPayload,_operator_request_id:id})});result=await response.json();}
          catch(_){const saved=await read(id,kind,captured);if(saved)return {status:'ok',acceptance:saved};throw Error('Подтверждение ещё не получено. Повторно отправлять не нужно.');}
          if(!response.ok){
            if([400,409,422].includes(response.status)&&result.source_not_saved===true&&result.operation_id===id){removeOwnRejected(id,kind);container.hidden=true;throw Error(String(result.error||'Изменение не сохранено.'));}
            const saved=await read(id,kind,captured);if(saved)return {status:'ok',acceptance:saved};throw Error('Подтверждение ещё не получено. Повторно отправлять не нужно.');
          }
          const verified=await OperatorAcceptance.readSameOperation({operation_id:id,domain:kind},async()=>result);
          if(verified.status!=='accepted'){const saved=await read(id,kind,captured);if(saved)return {status:'ok',acceptance:saved};throw Error('Подтверждение ещё не получено. Повторно отправлять не нужно.');}
          known.set(id,verified.operation);unknown.delete(id);show(verified.operation);return result;
        });
      }finally{busy=false;}
    }
    function onReturn(){restore();}
    window.addEventListener('storage',event=>{if(event.key===null||DOMAINS.some(kind=>event.key===key(kind)))onReturn();});
    window.addEventListener('pageshow',onReturn);window.addEventListener('focus',onReturn);
    document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')onReturn();});
    return Object.freeze({submit,restore});
  }
  window.OperatorPolicy=Object.freeze({create});
}());
