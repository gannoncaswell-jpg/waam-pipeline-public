
(function(root){
  'use strict';
  function create(env){
    const doc=env.document, el=id=>doc.getElementById(id);
    let identity=null, current=null, inspection=null, inspectionError='';
    let entries=[], revision=0, listRevision=0, pending=null, identityDone=false, loadingList=false;
    let resolveIdentity;
    const ready=new Promise(resolve=>{resolveIdentity=resolve;});
    const owner=()=>identity?.library_owner||'';
    const blocked=()=>env.blocked?.()||'';
    const ownWorkspace=()=>owner()?'library-'+owner():'';
    const key=entry=>entry.source_owner+'/'+entry.id;
    function feedback(text,error=false){
      el('libraryStatus').textContent=text;
      el('libraryStatus').dataset.state=error?'error':'info';
      env.feedback(text,error);
    }
    function invalidate(){
      revision++;listRevision++;loadingList=false;current=null;entries=[];inspection=null;inspectionError='';
      el('librarySelect').replaceChildren();el('librarySelect').disabled=true;
      env.adopt(null,{kind:'none'});paint();
    }
    async function request(path,params={},body=null,method='POST'){
      const expected=owner();
      const query=new URLSearchParams(params);
      if(expected)query.set('owner',expected);
      const response=await env.fetch(env.api(path+(query.size?'?'+query:'')),
        method==='GET'?{}:{method,body});
      let result;
      try{result=await response.json();}
      catch(e){throw new Error(response.status===401?'Your sign-in expired. Sign in again, then reopen the library.':'The server did not return a complete response. Please retry.');}
      if(expected!==owner())throw new Error('The account changed. Reopen the library.');
      if([401,403,409].includes(response.status) && expected)invalidate();
      if(!response.ok || !result.ok)throw new Error(result.error||'The request failed. Please retry.');
      if(result.owner_id && result.owner_id!==expected)throw new Error('The account changed. Refresh the page.');
      return result;
    }
    function queue(action){
      const job=(pending||Promise.resolve()).then(action).catch(error=>{
        feedback(error.message||String(error),true);return false;
      });
      pending=job;
      job.finally(()=>{if(pending===job)pending=null;});
      return job;
    }
    function paint(){


      const own=current?.kind==='stl' && current.source_owner===ownWorkspace();
      el('libraryDownloadRow').hidden=!own;
      el('libraryDownload').href=own?env.api('/api/library/file?'+new URLSearchParams({id:current.id,owner:owner()})):'#';
      const box=el('stlInspection');
      box.hidden=!inspection && !inspectionError;
      box.dataset.state=inspectionError?'warning':'info';
      if(inspectionError){box.textContent=inspectionError;return;}
      if(inspection){
        const names={mm:'millimetres',in:'inches',m:'metres'};
        const size=inspection.size_mm||[];
        const estimated=inspection.estimated ?? ((inspection.requested_units||el('wbUnits').value)==='auto');
        const dimensions=size.map(v=>Number(v).toFixed(Number(v)<1?3:2)).join(' × ');
        box.textContent=(estimated?'Auto estimate: ':'Units: ')+(names[inspection.resolved_units]||inspection.resolved_units)+
          ' → '+dimensions+' mm.\n'+(estimated?'Confirm the dimensions match your CAD model.':'');
        const height=Number(size[2]), layer=Number(el('wbLh').value);
        if(height>0 && height<layer){
          box.dataset.state='warning';
          box.textContent+='\nImported height '+height.toPrecision(3)+' mm is below the '+layer+' mm layer height, before tilt. Check units or layer height.';
        }
      }
      if(current){
        el('sourceInfo').textContent=current.name+(current.kind==='sample'?' · starter part':' · saved in your library');
      }else if(owner() && !env.file() && !env.loadedName()){
        el('sourceInfo').textContent='Choose a library part or upload an STL.';
      }
    }
    function showInspection(result){
      inspection=result?.inspection||result?.stl||null;
      inspectionError=result?.inspection_error||'';
      paint();
    }
    function options(){
      const select=el('librarySelect'), query=el('librarySearch').value.trim().toLowerCase();
      select.replaceChildren();
      const blank=doc.createElement('option');blank.value='';blank.textContent='Choose a library part';select.appendChild(blank);
      for(const entry of entries){
        const label=entry.name+(el('libraryScope').value==='all'?
          ' · '+(entry.owner||'Legacy workspace '+entry.source_owner):'');
        if(query && !label.toLowerCase().includes(query))continue;
        const option=doc.createElement('option');option.value=key(entry);option.textContent=label;
        select.appendChild(option);
      }
      if(current && entries.some(e=>key(e)===key(current)))select.value=key(current);
      select.disabled=!!env.busy() || loadingList || !!blocked();
    }
    function adopt(entry){
      current=entry;
      if(entry){
        el('wbUnits').value=entry.units||'auto';
        if(entry.kind==='sample')el('wbShape').value=entry.shape;
        const previous=entries.findIndex(e=>key(e)===key(entry));
        if(previous<0)entries.unshift(entry);else entries[previous]=entry;
      }
      env.adopt(entry,entry?{kind:'library',id:entry.id,units:entry.units||'auto'}:{kind:'none'});
      options();showInspection(entry);paint();
    }
    async function choose(entry){
      const ticket=++revision;
      return queue(async()=>{
        await ready;
        if(ticket!==revision || !owner())return false;
        if(!env.begin('Loading library part…'))return false;
        try{
          let selected=entry;
          if(entry.source_owner!==ownWorkspace()){
            const result=await request('/api/admin/library/import',{source_owner:entry.source_owner,id:entry.id});
            selected=result.entry;
          }else if(entry.kind==='stl'){
            const result=await request('/api/library/units',{id:entry.id,units:entry.units||'auto'});
            selected=result.entry;
          }
          if(ticket!==revision)return false;
          adopt(selected);
          feedback(selected.name+(entry.source_owner!==ownWorkspace()?' copied into your library.':' selected.')+' Adjust the settings, then slice the part.');
          return true;
        }finally{env.end();}
      });
    }
    async function refresh(initial=false){
      if(!owner())return;
      const ticket=++listRevision, sourceRevision=revision;
      loadingList=true;
      el('librarySelect').disabled=true;
      try{
        const path=identity.admin && el('libraryScope').value==='all'?'/api/admin/library':'/api/library';
        const result=await request(path,{},null,'GET');
        if(ticket!==listRevision)return;
        entries=result.entries;options();
        el('libraryStatus').textContent=result.warnings?'Some stored files could not be verified. Contact the administrator.':'';
        if(initial && !env.file()){
          const state=env.state();
          const draft=env.draftSource?.();


          const selectedId=draft ? (draft.kind==='library'?draft.id:null) :
            state.meta.params?.library || (!env.loadedName()?'sample:'+(state.meta.shape||'wall'):null);
          const entry=entries.find(e=>e.id===selectedId && e.source_owner===ownWorkspace());
          const draftRestore=draft?.kind==='library'&&draft.id===entry?.id;
          if(entry&&(draftRestore||sourceRevision===revision&&!current)){

            const units=draft?.kind==='library'&&draft.id===entry.id?
              el('wbUnits').value:state.meta.params?.units;
            adopt(entry.kind==='stl' && ['auto','mm','in','m'].includes(units)?{...entry,units}:entry);
          }
        }
      }catch(error){
        if(ticket===listRevision){feedback(error.message,true);el('librarySelect').disabled=false;}
      }finally{if(ticket===listRevision){loadingList=false;update();}}
    }
    async function inspectSource(fresh=false){
      if(blocked()){feedback(blocked(),true);return false;}
      const file=env.file(), ticket=++revision;
      if(fresh){
        current=null;inspection=null;inspectionError='';
        el('wbUnits').value='auto';
        env.sourceChanged();paint();
      }
      return queue(async()=>{
        await ready;
        if(identity?.unavailable){feedback('Could not confirm your sign-in. Refresh the page and retry the upload.',true);return false;}
        if(ticket!==revision || env.file()!==file)return false;
        if(!file && !current && !env.loadedName())return true;
        if(file && !/\.stl$/i.test(file.name)){
          feedback('Choose a binary or ASCII .stl file.',true);return false;
        }
        if(file && file.size>64*1024*1024){feedback('STL exceeds the 64 MB limit.',true);return false;}
        if(!env.begin(file && owner()?'Saving STL and checking units…':'Checking STL units…'))return false;
        try{
          const units=el('wbUnits').value;
          const body=file?await file.arrayBuffer():null;


          if(ticket!==revision || env.file()!==file)return false;
          let result;
          if(file && owner()){
            result=await request('/api/library/upload',{name:file.name,units},body);
          }else if(current?.kind==='stl' && owner()){
            result=await request('/api/library/units',{id:current.id,units});
          }else{
            result=await request('/api/slice',{kind:'stl',inspect:'1',units,reuse:file?'0':'1'},body);
          }
          if(ticket!==revision || (file && env.file()!==file))return false;
          if(result.entry)adopt(result.entry);else showInspection(result);
          const dimensions=inspection?.size_mm?.map(v=>Number(v).toFixed(2)).join(' × ');
          feedback(inspectionError || (result.entry?'Saved. ':'')+(dimensions?dimensions+' mm. ':'')+'Confirm the units, then slice the part.',!!inspectionError);
          return !inspectionError;
        }finally{env.end();}
      });
    }
    async function setIdentity(who){
      const changed=identity && owner()!==(who.library_owner||'');
      if(changed)invalidate();
      identity=who;
      el('demoPicker').hidden=!!owner();
      el('libraryPanel').hidden=!owner();
      el('libraryScope').hidden=!who.admin;
      el('clearStl').title=owner()?'Choose another library part':'Use the demo part';
      if(!identityDone){identityDone=true;resolveIdentity();}
      if(owner())await refresh(true);
      paint();
    }
    function clear(){
      revision++;current=null;inspection=null;inspectionError='';options();paint();
    }
    function restoreSource(source){
      revision++;inspection=null;inspectionError='';
      if(source?.kind==='library'){
        current=entries.find(entry=>entry.id===source.id&&entry.source_owner===ownWorkspace())||null;
        if(current)current={...current,units:source.units||'auto'};
        else if(owner())current={kind:'stl',id:source.id,source_owner:ownWorkspace(),
          name:'Library part '+source.id.slice(0,8),units:source.units||'auto'};
      }else current=null;
      options();paint();return current;
    }
    async function prepareSlice(){
      await ready;
      if(identity?.unavailable){feedback('Could not confirm your sign-in. Refresh the page before slicing.',true);return false;}
      if(pending && !await pending)return false;
      if(env.file() && (!inspection || owner() && !current)){
        if(!await inspectSource(false))return false;
      }
      if(owner() && !current && !env.file() && !env.loadedName()){
        feedback('Choose a library part or upload an STL before slicing.',true);return false;
      }
      return true;
    }
    function params(){
      if(!owner())return {};
      return {owner:owner(),...(current?{library:current.id}:{})};
    }
    function sliced(result){
      if(result.library_entry && owner() && result.library_entry.source_owner===ownWorkspace())adopt(result.library_entry);
      if(result.state?.meta.stl){


        paint();
        if(current)el('libraryStatus').textContent='Saved in your library. Adjust settings to slice again.';
      }
    }
    function update(){el('librarySelect').disabled=!!env.busy() || loadingList || !!blocked();paint();}
    el('libraryRefresh').onclick=()=>refresh();
    el('libraryScope').onchange=()=>refresh();
    el('librarySearch').oninput=options;
    el('librarySelect').onchange=()=>{
      if(blocked()){feedback(blocked(),true);options();return;}
      const entry=entries.find(e=>key(e)===el('librarySelect').value);
      if(entry)choose(entry);
    };
    el('wbUnits').addEventListener('change',()=>inspectSource(false));
    el('wbLh').addEventListener('input',paint);
    return {setIdentity,clear,restoreSource,paint,prepareSlice,params,sliced,update,
      fileChanged:()=>inspectSource(true),pending:()=>!!pending};
  }
  const api={create};
  if(typeof module==='object' && module.exports)module.exports=api;
  root.WaamLibrary=api;
})(typeof window==='undefined'?globalThis:window);
