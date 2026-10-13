(function(root){
  "use strict";
  function create(env){
    const get=id=>env.document.getElementById(id);
    const controls=["wbAutoSpacing","wbAutoMax","wbAutoMaxUnit","wbAutoAmbient","wbAutoBefore","wbAutoAfter",
      "wbAutoUnit","wbAutoVarBtn","wbAutoVarFile","wbAutoReading","wbAutoReadingUnit","wbAutoReadingBtn"];
    let busy=false,message="",hold="",latest=null,pending=null,runId=null,loaded=false,refreshVersion=0;
    let cooling="",proposal=null,coolingVersion=0;
    const prefilled={};
    const toUnit=c=>get("wbAutoMaxUnit").value==="F"?c*9/5+32:c;

    const CLEARED="waam.autoMaxCleared";
    const storage=(()=>{try{return env.storage!==undefined?env.storage:root.sessionStorage||null;}catch(e){return null;}})();
    let cleared=false;
    try{cleared=storage?.getItem(CLEARED)==="1";}catch(e){                                          }
    function rememberClear(value){
      cleared=value;
      try{if(value)storage?.setItem(CLEARED,"1");else storage?.removeItem(CLEARED);}catch(e){                     }
    }

    function prefill(id,celsius){
      const field=get(id);
      if(id==="wbAutoMax"&&cleared)return;
      if(field.value.trim()!==""&&field.value!==prefilled[id])return;
      field.value=prefilled[id]=Number.isFinite(celsius)?String(Math.round(toUnit(celsius))):"";
    }
    function prefillAll(){
      const filler=get("wbFiller"),option=filler?.querySelector(`option[value="${filler.value}"]`);
      const raw=option?.getAttribute("data-interpass-c");
      prefill("wbAutoMax",raw==null?NaN:Number(raw));prefill("wbAutoAmbient",25);
    }
    const enabled=()=>get("wbBuildMode").value==="auto";
    const supported=()=>env.state().meta.params?.cell_profile==="lc50s"&&
      env.state().meta.clearance_checked===true&&!env.state().meta.cell_profile?.planning_only&&
      env.state().meta.params?.cell!=="0"&&env.state().meta.cell!=="workbench"&&
      !(env.state().meta.shape==="bead"&&!env.state().meta.stl&&Number(env.state().meta.params?.bead_count||1)>1);
    const modeReady=()=>!env.guest()&&!env.busy()&&!busy&&!env.blocked?.();
    const available=()=>supported()&&modeReady();
    const ready=()=>enabled()&&available();
    function sets(){
      const values=JSON.parse(get("wbLayerSense").value||"[]");
      if(!Array.isArray(values))throw new Error("The layer touch sets are not a list.");
      return values;
    }
    function next(){
      const auto=env.state().meta.auto;
      if(!auto)return null;
      const n=Number(get("wbInterpassEvery").value);
      if(!Number.isInteger(n)||n<1||n>200)return null;
      return {cycle:auto.cycles_done+1,firstLayer:auto.next_first,lastLayer:Math.min(auto.next_first+n-1,auto.top_layer),
        step:auto.step_mm,complete:auto.next_first>auto.top_layer};
    }
    const mm=value=>Number(value).toFixed(2);
    function fitText(info){
      const touched=info.touched===undefined?"":`${info.touched} of ${info.planned} touched`+
        (info.missed?.length?` (missed ${info.missed.join(", ")})`:"")+"; ";
      return `Cycle ${info.cycle}: ${touched}top ${mm(info.mean_error_mm)} mm on average; growth ${mm(info.growth_mm)} mm per layer `+
        `(raw ${mm(info.growth_raw_mm)}, ${info.clamped?"clamped":"not clamped"}). Next cycle steps ${mm(info.growth_mm)} mm per layer.`+
        (info.warnings?.length?` Warning: ${info.warnings.join(" ")}`:"");
    }
    function paint(){
      const auto=enabled(),ok=available(),cycle=next();
      get("wbBuildManual").checked=!auto;get("wbBuildAuto").checked=auto;
      get("wbBuildManual").disabled=!modeReady();get("wbBuildAuto").disabled=!ok;
      get("wbJbiName").maxLength=auto?28:32;
      get("wbJbiName").pattern=auto?"[A-Z0-9][A-Z0-9_\\-]{0,27}":"[A-Z0-9][A-Z0-9_\\-]{0,31}";
      get("jobNameHint").textContent=auto?
        "1–28 characters: letters, digits, hyphen or underscore. Start with a letter or digit; the cycle suffix is added.":
        "1–32 characters: letters, digits, hyphen or underscore. Start with a letter or digit.";
      get("autoOptions").hidden=get("autoPanel").hidden=!auto;
      get("wbManualStages").disabled=auto;
      get("wbPrintLayers").disabled=auto;
      get("autoManualHint").hidden=!auto;
      for(const id of controls)get(id).disabled=!auto||!ok;
      const canImport=auto&&ok&&latest&&!latest.last_cycle&&latest.status!=="applied";
      get("wbAutoBefore").disabled=get("wbAutoAfter").disabled=get("wbAutoUnit").disabled=!canImport||!!pending;
      get("wbAutoVarBtn").disabled=get("wbAutoVarFile").disabled=!canImport;
      get("wbAutoVarBtn").textContent=pending?"Apply imported cycle":"Import cycle VAR.DAT (P120…)";
      for(const id of ["wbAutoBefore","wbAutoAfter"])get(id).max=get("wbAutoUnit").value==="F"?"2500":"1400";
      get("wbAutoMax").max=get("wbAutoMaxUnit").value==="F"?"2500":"1400";
      get("wbAutoAmbient").max=get("wbAutoMaxUnit").value==="F"?"140":"60";
      get("autoCooling").textContent=auto?cooling:"";
      get("wbAutoUseDwell").hidden=!auto||!proposal;get("wbAutoUseDwell").disabled=!ok||!proposal;
      if(proposal)get("wbAutoUseDwell").textContent=`Use proposed dwell (${proposal.seconds} s)`;
      get("wbAutoReading").max=get("wbAutoReadingUnit").value==="F"?"2500":"1400";
      get("emitBtn").textContent=auto?(cycle?(cycle.complete?"The part is complete. Finish the run.":
        `Export cycle ${cycle.cycle} · layers ${cycle.firstLayer}-${cycle.lastLayer}`):"Export Auto cycle"):
        "Export robot job · .JBI";
      if(auto&&(!ok||!cycle||cycle.complete))get("emitBtn").disabled=true;
      if(auto)get("emitRosBtn").disabled=true;
      get("autoNext").textContent=cycle?(cycle.complete?"The part is complete. Finish the run.":
        `Next cycle ${cycle.cycle}: layers ${cycle.firstLayer}-${cycle.lastLayer}, ${mm(cycle.step)} mm per layer.`):"Slice and check an LC build to prepare Auto cycles.";
      const h=Number(env.state().meta.params?.lh),n=cycle?cycle.lastLayer-cycle.firstLayer+1:0;
      get("autoWarning").textContent=cycle?.cycle===1&&n>0&&Number.isFinite(h)?
        `Cycle 1 predicts the top from the Bead height (${h} mm). A layer growing more than ${Number((75/n).toFixed(2))} % less than that misses its touches, and three misses in a row stop the job (alarm 8814); more than 50 % more and the wire may drag between touches. Set Bead height to what your last wall really grew.`:"";
      get("autoSteps").textContent=latest?(latest.last_cycle?
        "Last cycle: no touches. When it has run, finish the run.":latest.status==="applied"?"":
        `Run ${latest.job}. At each pause read the temperature, then press START. When it ends, save VAR.DAT and import it here.`):"";
      const fits=env.state().meta.layer_sense||[],info=latest?.summary||fits.filter(item=>Number.isFinite(item.growth_mm)).at(-1);
      get("autoFit").textContent=info?fitText(info):"";
      get("autoHold").hidden=!hold;get("autoHoldMessage").textContent=hold;
      get("autoStatus").textContent=env.blocked?.()||message;
    }
    async function json(path,options){
      const response=await env.request(path,options),data=await response.json().catch(()=>null);
      if(!response.ok||!data?.ok)throw new Error(data?.error||`Auto cycle request failed (HTTP ${response.status}).`);
      return data;
    }
    async function refresh(){
      if(busy)return;
      const version=++refreshVersion;
      if(env.guest()){latest=null;pending=null;paint();return;}
      try{
        const data=await json("/api/run");
        if(version!==refreshVersion)return;
        const id=data.active?.run||null;
        if(loaded&&id!==runId){pending=null;hold="";message="";}
        runId=id;loaded=true;latest=data.active?.cycles?.at(-1)||null;
        pending=latest?.status==="imported"&&Array.isArray(latest.records)?
          {records:latest.records,hits:latest.hits??null,base:latest.sense_base,
            flag_base:latest.flag_base,summary:latest.summary}:null;
      }catch(e){if(version===refreshVersion)message=e.message;}
      paint();
      void coolingFit();
    }

    async function coolingFit(){
      const version=++coolingVersion,unit=get("wbAutoMaxUnit").value;
      const setpoint=get("wbAutoMax").value.trim();
      proposal=null;
      if(!enabled()||env.guest()){cooling="";paint();return;}
      if(!runId){cooling="Cooling fit: start a run to record cooling measurements.";paint();return;}
      if(!setpoint){cooling="Cooling fit: enter a max interpass to get a dwell proposal.";paint();return;}
      try{
        const query=env.query({setpoint,unit,ambient:get("wbAutoAmbient").value.trim()});
        const data=await json("/api/run/cooling?"+query);
        if(version!==coolingVersion)return;
        const f=data.fit,p=data.proposal,deg=c=>`${Math.round(toUnit(c))} °${unit}`;
        const sheet=(data.basis||f.basis||p.basis)==="layer_decay",basis=sheet?"sheet decay fit":"cycle pairs";
        const count=sheet?`${f.count} ${f.count===1?"row":"rows"}`:`${f.used.length} cycles`;
        cooling=`Cooling fit (${basis}, ${count}): tau ${Math.round(f.tau_s)} s, RMS ${(f.rms_c*(unit==="F"?1.8:1)).toFixed(1)} °${unit}`+
          `${f.valid?"":" (does not fit one exponential well)"}. Proposed interpass dwell ${p.seconds} s, `+
          (sheet?"from ":"for a T-before like the last ")+
          `(${deg(p.start_c)}) to cool to ${deg(p.setpoint_c)}.`+
          (p.hold_s>0?` The ${sheet?"latest reading":"last T-after"} needs about ${p.hold_s} s more.`:"")+
          (p.notes.length?" Note: "+p.notes.join("; ")+".":"")+
          (sheet?" The max interpass hold still checks the next reading.":
            " The fit cannot see the time spent at the pauses, so tau reads slightly short and the proposal "+
            "slightly optimistic; the max interpass hold still checks the next reading.");
        proposal=p;
      }catch(e){if(version===coolingVersion)cooling="Cooling fit: "+e.message;}
      if(version===coolingVersion)paint();
    }
    function temperature(id,unit){
      const field=get(id),value=Number(field.value),limit=unit==="F"?2500:1400;
      if(!field.value.trim()||!Number.isFinite(value)||value< -40||value>limit)
        throw new Error(`Enter a temperature from -40 to ${limit} °${unit}.`);
      return value;
    }
    function options(){
      const spacing=Number(get("wbAutoSpacing").value);
      if(!Number.isFinite(spacing)||spacing<5||spacing>100)throw new Error("Layer touch spacing: enter 5 to 100 mm.");
      if(sets().some(item=>item.step!==true))
        throw new Error("Auto cycles re-step every later layer; remove the manual layer touch sets first, or stay in Manual");
      const maximum=get("wbAutoMax").value.trim();
      return {spacing,...(maximum?{max_interpass:temperature("wbAutoMax",get("wbAutoMaxUnit").value),max_unit:get("wbAutoMaxUnit").value}:{})};
    }
    async function perform(start,step){
      if(!ready())return;
      refreshVersion++;
      busy=true;message=start;paint();
      try{message=await step();}
      catch(e){message=e.message;if(message.startsWith("Hold:")){hold=message;get("wbAutoReadingUnit").value=get("wbAutoMaxUnit").value;}}
      finally{busy=false;paint();env.changed?.();}
    }
    async function exportCycle(){
      return perform("Preparing the cycle job…",async()=>{
        const o=options(),result=await env.exportCycle(o);
        latest={cycle:result.cycle,layers:[result.firstLayer,result.lastLayer],last_cycle:result.last_cycle,
          job:result.job,spacing_mm:o.spacing,status:"exported"};
        pending=null;hold="";
        get("wbAutoBefore").value=get("wbAutoAfter").value="";
        const note=(result.notes?` Note: ${result.notes}.`:"")+
          (result.archive==="failed"?" Server copy failed. Keep the downloaded file.":"")+
          (result.run==="failed"?" Run filing failed. Keep the downloaded file.":"");
        return `Downloaded ${result.job}.JBI.${result.passes||""}${note}`;
      });
    }
    async function applyPending(){
      const prior=sets();
      if(!prior.some(item=>item.step===true&&item.layer===latest.layers[1])){
        env.pause?.();

        const flags=Array.isArray(pending.hits)?
          {base:pending.base,flag_base:pending.flag_base,hits:pending.hits}:{};
        const values=[...prior,{layer:latest.layers[1],spacing:latest.spacing_mm,points:pending.records,step:true,...flags}];
        if(!await env.apply(JSON.stringify(values)))
          throw new Error("Cycle touches were imported but not applied. Review the placement message, then choose Apply imported cycle.");
      }
      const applied=await json("/api/run/cycle-applied?"+env.query({cycle:latest.cycle}),{method:"POST"});
      latest.status="applied";latest.summary=applied.summary||pending.summary;pending=null;
      env.refreshRun?.();
      return `Cycle ${latest.cycle} touches applied. Export the next cycle when the temperature allows it.`;
    }
    const file=get("wbAutoVarFile");
    get("wbAutoVarBtn").onclick=()=>{
      if(!ready()||!latest||latest.last_cycle||latest.status==="applied")return;
      if(pending)return perform("Applying imported cycle…",applyPending);
      file.click();
    };
    file.onchange=()=>{
      const chosen=file.files&&file.files[0];
      if(!chosen){file.value="";return Promise.resolve();}
      if(!ready()){file.value="";paint();return Promise.resolve();}
      return perform("Importing cycle touches and temperatures…",async()=>{
        try{
          if(!latest||latest.last_cycle||latest.status!=="exported")throw new Error("Export a cycle with touches before importing VAR.DAT.");
          const unit=get("wbAutoUnit").value;
          const query={cycle:latest.cycle,t_before:temperature("wbAutoBefore",unit),t_after:temperature("wbAutoAfter",unit),unit};
          pending=await json("/api/cycle-import?"+env.query(query),{method:"POST",body:await(chosen.arrayBuffer?chosen.arrayBuffer():chosen.text())});
          latest.status="imported";latest.summary=pending.summary;
          return await applyPending();
        }finally{file.value="";}
      });
    };
    get("wbAutoReadingBtn").onclick=()=>perform("Recording temperature…",async()=>{
      const unit=get("wbAutoReadingUnit").value,value=temperature("wbAutoReading",unit);
      await json("/api/run/temperature",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({value,unit,stage:"before-weld",note:""})});
      get("wbAutoReading").value="";hold="";env.refreshRun?.();
      return `Recorded ${value} °${unit}. Export again to check the maximum interpass temperature.`;
    });
    get("wbAutoUseDwell").onclick=()=>{
      if(!proposal||!available())return;
      const field=get("wbInterpass");field.value=String(proposal.seconds);
      if(field.dispatchEvent)field.dispatchEvent(new Event("input"));else field.dispatch?.("input");
      env.changed?.();
      message=`Interpass dwell set to ${proposal.seconds} s for the next cycle export.`;paint();
    };
    for(const [id,value] of [["wbBuildManual","manual"],["wbBuildAuto","auto"]])get(id).addEventListener("change",()=>{
      if(!modeReady()||(value==="auto"&&!supported()))return;
      get("wbBuildMode").value=value;paint();env.changed?.();
      if(value==="auto")void refresh();
    });
    get("wbAutoMax").addEventListener("input",()=>rememberClear(get("wbAutoMax").value.trim()===""));
    for(const id of ["wbInterpassEvery","wbAutoSpacing","wbAutoMax","wbAutoMaxUnit","wbAutoUnit","wbAutoReadingUnit","wbAutoAmbient"]){
      const edited=()=>{
        if(id==="wbAutoMaxUnit")prefillAll();
        paint();if(id==="wbInterpassEvery")env.changed?.();
        if(["wbAutoMax","wbAutoMaxUnit","wbAutoAmbient"].includes(id))void coolingFit();
      };
      get(id).addEventListener("input",edited);get(id).addEventListener("change",edited);
    }
    get("wbFiller")?.addEventListener("change",()=>{prefillAll();paint();void coolingFit();});
    prefillAll();
    get("autoPanel").addEventListener("toggle",()=>{if(get("autoPanel").open&&!busy&&!env.busy())void refresh();});
    paint();
    return {paint,refresh,enabled,exportCycle};
  }
  root.WaamAuto={create};
})(typeof module==="object"&&module.exports?module.exports:window);
