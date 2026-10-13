(function(root){
  "use strict";
  function record(rows, ctwd, options){
    const corners=rows.map((row,i)=>{
      const tokens=row.pulses.trim().split(/[\s,;]+/);
      if(tokens.length!==6 || tokens.some(v=>!/^[-+]?\d+$/.test(v) || !Number.isSafeInteger(Number(v))))
        throw new Error(`Corner ${i+1}: enter six whole PULSE values in S, L, U, R, B, T order.`);
      const distance=Number(row.distance);
      if(!row.distance.trim() || !Number.isFinite(distance) || distance<0 || distance>100)
        throw new Error(`Corner ${i+1}: enter a contact-tip gap from 0 to 100 mm.`);
      return {pulses:tokens.map(Number),tip_distance_mm:distance};
    });
    if(!Number.isFinite(ctwd)||ctwd<=0) throw new Error("The cell profile's contact-tip distance is unavailable.");
    const result={version:1,frame:"lc50s_tool0_pulse",weld_ctwd_mm:ctwd,corners};
    if(options){
      if(!["normal","vertical","torch"].includes(options.gap_direction))
        throw new Error("Choose the direction in which the contact-tip gap was measured.");
      const offset=Number(options.tool0_tip_offset_mm);
      if(String(options.tool0_tip_offset_mm??"").trim()==="" || !Number.isFinite(offset) || offset< -100 || offset>100){
        const error=new Error("Tool setup required: enter the verified contact-tip to TOOL 0 distance from -100 to 100 mm. If it is unknown, have the calibration owner measure it before applying the surface.");
        error.field="wbTool0TipOffset";throw error;
      }
      Object.assign(result,{version:2,gap_direction:options.gap_direction,tool0_tip_offset_mm:offset});
    }
    return result;
  }
  function create(env){
    const get=id=>env.document.getElementById(id);
    const fileBody=file=>file.arrayBuffer?file.arrayBuffer():file.text();
    const fields=["wbSubstrateGapDirection","wbTool0TipOffset",...Array.from({length:4},(_,i)=>["wbCorner"+(i+1),"wbTipGap"+(i+1)]).flat()];
    const senseFields=["wbSenseLayout","wbSenseFit","wbSenseSpacing","wbSenseInset","wbSensePoints","wbSenseName"];
    const touch=get("wbTouchCorners");
    let accepted=null,registration=null,dirty=false,message="",senseBusy=false,senseMessage="",senseJob=null;
    const blocked=()=>env.blocked?.()||"";
    function pending(){return dirty||!!env.state().meta.registration_pending;}
    function error(){return pending()?"Substrate registration is pending. Register the corner entries or clear registration before saving or exporting.":"";}
    function touching(){return !!touch?.checked;}
    function surfaceRecord(){
      if(!registration)return null;
      const {sensed,...base}=registration;
      return {...base,version:2};
    }
    function surfaceKey(){return JSON.stringify(surfaceRecord());}
    function senseReady(){
      return !!env.state().meta.substrate?.gap_direction&&[2,3].includes(registration?.version)&&
        !pending()&&!env.busy()&&!senseBusy&&!env.guest?.()&&!blocked();
    }
    function senseCount(value){
      const count=Number(value);
      if(!Number.isInteger(count)||count<2||count>21)throw new Error("Enter a sensed touch count from 2 to 21.");
      return count;
    }
    const pvars=(base,count)=>`P${String(base).padStart(3,"0")}–P${String(base+count-1).padStart(3,"0")}`;


    function syncTouch(){
      const on=touching();
      if(on)get("wbSubstrateGapDirection").value="torch";
      for(let i=1;i<=4;i++){
        const gap=get("wbTipGap"+i);gap.readOnly=on;
        if(on)gap.value=get("wbTool0TipOffset").value.trim();
      }
    }
    function remember(){
      try{env.storage?.setItem(env.storageKey,JSON.stringify({gen:env.state().meta.gen,pending:dirty,message,touch:touching(),values:Object.fromEntries(fields.map(id=>[id,get(id).value])),
        senseJob,senseRegistration:surfaceKey(),senseValues:Object.fromEntries(senseFields.map(id=>[id,get(id).value]))}));}catch(e){}
    }
    function changed(){remember();paint();env.changed?.();}
    function paint(){
      const state=env.state(), data=state.meta.substrate, params=state.meta.params||{};
      const active=!!data, allowed=params.cell_profile==="lc50s";
      const raw=params.substrate_registration||"";
      const writeBlocked=!!blocked();
      get("registerSubstrate").disabled=env.busy()||senseBusy||writeBlocked||!allowed;
      get("clearSubstrate").disabled=env.busy()||senseBusy||writeBlocked||(!active&&!pending());
      get("wbVarDatBtn").disabled=get("wbVarDatFile").disabled=
        env.busy()||senseBusy||writeBlocked||!env.importVarDat;
      get("registrationCtwd").textContent=Number.isFinite(state.meta.registration_ctwd_mm)?`${state.meta.registration_ctwd_mm} mm`:"Unavailable";
      if(raw!==accepted){
        const previous=surfaceKey();
        accepted=raw;dirty=false;message="";
        get("wbTool0TipOffset").removeAttribute("aria-invalid");get("substrateToolOffsetError").hidden=true;
        registration=null;
        try{registration=JSON.parse(raw);}catch(e){}
        get("wbSenseFit").value=registration?.sensed?.fit||"smooth";
        if(previous!==surfaceKey()){senseJob=null;senseMessage="";}
        get("wbSubstrateGapDirection").value=registration?(registration.gap_direction||"vertical"):"normal";
        get("wbTool0TipOffset").value=registration?.tool0_tip_offset_mm===undefined?"":String(registration.tool0_tip_offset_mm);
        for(let i=1;i<=4;i++){
          const row=registration?.corners[i-1];
          get("wbCorner"+i).value=row?row.pulses.join(", "):"";
          get("wbTipGap"+i).value=row?String(row.tip_distance_mm):"";
        }
        if(touch)touch.checked=registration?.gap_direction==="torch"&&
          registration.corners.every(row=>row.tip_distance_mm===registration.tool0_tip_offset_mm);
        syncTouch();
      }
      const sensed=active&&registration?.version===3;
      const fit=sensed?data.sensed:null;
      get("substrateIntro").textContent=sensed?"The part is seated on the sensed surface. Review the fit and footprint below.":allowed?"Enter four PULSE readings and ruler gaps. Calculate the surface height and footprint, then seat the part automatically.":"Select the LC50S cell, then record four corners.";
      const tilts=data?.torch_tilt_deg;
      const tiltText=tilts?.length?` Recorded torch tilt ${Math.min(...tilts).toFixed(2)}–${Math.max(...tilts).toFixed(2)}° from vertical.`:"";
      const needsSetup=!get("wbTool0TipOffset").value.trim();
      get("substrateToolSetupStatus").textContent=needsSetup?"One-time tool setup required: add the verified TOOL 0 tip distance in Measurement & tool setup.":"Tool distance entered. Confirm it belongs to the current TOOL 0 calibration.";
      const direction=get("wbSubstrateGapDirection").value;
      get("substrateGapHint").textContent=touching()?"The wire tip touched each corner, so each gap is the TOOL 0 distance along the wire axis.":direction==="normal"?"Measure each gap perpendicular to the substrate, from the metal contact tip to the top surface.":direction==="vertical"?"Measure each gap straight down vertically, from the metal contact tip to the top surface.":"Measure each gap along the wire axis, from the metal contact tip to the top surface.";
      const setup=data?.surface_setup;
      const mm=value=>Number.isFinite(value)?`${value.toFixed(2)} mm`:"—";
      const range=values=>Array.isArray(values)&&values.length===2&&values.every(Number.isFinite)?`${values[0].toFixed(2)} to ${values[1].toFixed(2)} mm`:"—";
      const bounds=setup?.bounds_xy_from_platter_center_mm;
      get("substrateReadout").dataset.state=pending()?"pending":data?"ready":"empty";
      get("substrateReadoutState").textContent=pending()?(data?"Previous surface · changes not applied":"Changes not applied · surface pending"):sensed?"Part seated on sensed surface":data?"Surface ready":"Surface not calculated";
      get("substrateHeight").textContent=mm(setup?.center_height_above_platter_mm);
      get("substrateHeightRange").textContent=range(setup?.height_range_above_platter_mm);
      get("substrateCoverageX").textContent=range(bounds?[bounds.min[0],bounds.max[0]]:null);
      get("substrateCoverageY").textContent=range(bounds?[bounds.min[1],bounds.max[1]]:null);
      get("substrateFootprint").textContent=data?`${data.size_mm.map(v=>v.toFixed(1)).join(" × ")} mm`:"—";
      get("substrateFitDetails").textContent=data?
        `${pending()?"Previous surface. ":""}Tilt ${data.tilt_deg.toFixed(2)}°. ${fit?.fit==="interpolated"?"Largest deviation from smooth fit":"Maximum fit error"} ${data.max_residual_mm.toFixed(3)} mm. Thickness ${data.thickness_mm.toFixed(2)} mm.`+tiltText:"Available after calculation.";
      get("substrateStatus").textContent=blocked()||(pending()?(message||error()):data?
        (sensed?"Surface ready. The part is seated on the sensed surface. Review its height and footprint below.":"Surface ready. The part is seated on the measured surface. Review its height and footprint below."):"Enter the four corners to calculate the surface.");
      const ready=senseReady();
      for(const id of senseFields)get(id).disabled=!ready;
      get("wbSenseJobBtn").disabled=!ready||!env.senseJob;
      get("wbSenseVarDatBtn").disabled=get("wbSenseVarDatFile").disabled=!ready||!env.importVarDat;
      get("wbSenseClearBtn").disabled=!ready||!sensed;
      const precise=value=>Number.isFinite(value)?`${value.toFixed(3)} mm`:"—";
      get("senseReadout").textContent=fit?
        `${fit.points} points · ${fit.fit} fit. `+(fit.fit==="interpolated"?
          `Maximum facet slope ${fit.max_facet_slope_deg.toFixed(2)}°. RMS deviation from smooth fit ${precise(fit.rms_residual_mm)}. Largest deviation from smooth fit ${precise(fit.max_residual_mm)}. `:
          `RMS residual ${precise(fit.rms_residual_mm)}. Maximum residual ${precise(fit.max_residual_mm)}. `)+
        `Worst point: ${fit.worst}. Height range: ${fit.height_range_mm.map(v=>v.toFixed(3)).join(" to ")} mm.`:"Not sensed";
      get("senseStatus").textContent=blocked()||senseMessage||(sensed?`Sensed touches: ${pvars(registration.sensed.base,registration.sensed.points.length)}.`:"Not sensed");
    }
    async function apply(clear=false){
      if(blocked()){paint();return false;}
      if(env.busy()||senseBusy)return false;
      dirty=true;message="Substrate registration is pending.";changed();
      try{
        if(!clear&&touching()&&!get("wbTool0TipOffset").value.trim()){
          const error=new Error("Wire tip touching uses the TOOL 0 distance as every gap. Tool setup required: enter the verified contact-tip to TOOL 0 distance.");
          error.field="wbTool0TipOffset";throw error;
        }
        const raw=clear?"":JSON.stringify(record(Array.from({length:4},(_,i)=>({pulses:get("wbCorner"+(i+1)).value,distance:get("wbTipGap"+(i+1)).value})),env.state().meta.registration_ctwd_mm,
          {gap_direction:get("wbSubstrateGapDirection").value,tool0_tip_offset_mm:get("wbTool0TipOffset").value}));
        const ok=await env.apply(raw);
        if(ok){dirty=false;message="";accepted=null;}
        else{dirty=true;message="Registration was not applied. Saving and export are blocked. Review the placement message below.";}
        changed();
        return ok;
      }catch(error){
        message=error.message;changed();
        if(error.field==="wbTool0TipOffset"){
          get("substrateToolSetup").open=true;
          get("substrateToolOffsetError").hidden=false;
          get("substrateToolOffsetError").textContent=error.message;
          get(error.field).setAttribute("aria-invalid","true");get(error.field).focus();
        }
        return false;
      }
    }
    get("registerSubstrate").onclick=()=>apply();
    get("clearSubstrate").onclick=()=>apply(true);
    for(const id of fields)get(id).addEventListener("input",()=>{
      env.pause();dirty=true;message="Measurements changed. Calculate & apply surface to use them; saving and export are blocked.";
      if(id==="wbTool0TipOffset"){
        get(id).removeAttribute("aria-invalid");get("substrateToolOffsetError").hidden=true;
      }

      if(id==="wbSubstrateGapDirection"&&touching()&&get(id).value!=="torch")touch.checked=false;
      syncTouch();
      changed();
    });
    touch?.addEventListener("change",()=>{
      env.pause();dirty=true;syncTouch();
      message=touching()?"Each gap is the TOOL 0 distance along the wire. Calculate & apply surface to use it; saving and export are blocked.":
        "Enter each ruler gap, then Calculate & apply surface; saving and export are blocked.";
      changed();
    });
    const varDatBtn=get("wbVarDatBtn"), varDatFile=get("wbVarDatFile");
    if(varDatBtn && varDatFile && env.importVarDat){
      varDatBtn.onclick=()=>{paint();if(!env.busy()&&!blocked())varDatFile.click();};
      varDatFile.onchange=async()=>{
        const file=varDatFile.files && varDatFile.files[0];
        if(!file||blocked()){varDatFile.value="";paint();return false;}
        const base=40;
        varDatBtn.disabled=true;
        try{
          const records=await env.importVarDat(await fileBody(file),base,4,"plate-corners");
          for(let i=0;i<4;i++) get("wbCorner"+(i+1)).value=records[i].join(", ");
          env.pause();dirty=true;
          message=`Corners filled from ${file.name} (P${String(base).padStart(3,"0")}–P${String(base+3).padStart(3,"0")}). Enter the four ruler gaps, then Calculate & apply surface.`;
          changed();
        }catch(e){ message="VAR.DAT import: "+e.message; changed(); }
        finally{ varDatBtn.disabled=false; varDatFile.value=""; }
      };
    }
    for(const id of senseFields)get(id).addEventListener("input",()=>{remember();paint();});
    get("wbSenseJobBtn").onclick=async()=>{
      paint();if(!senseReady()||!env.senseJob)return;
      senseBusy=true;senseMessage="Preparing the sense job…";paint();
      try{
        const options={...env.senseParams?.(),layout:get("wbSenseLayout").value,spacing:get("wbSenseSpacing").value,inset:get("wbSenseInset").value,
          points:senseCount(get("wbSensePoints").value),name:get("wbSenseName").value.trim()};
        const key=surfaceKey(),result=await env.senseJob(key,options);
        const count=senseCount(result.count);
        if(result.base!==44)throw new Error("The sense job did not report results starting at P044.");
        if(key!==surfaceKey())throw new Error("The registered surface changed. Download a new sense job.");
        senseJob={count,base:result.base,layout:options.layout};
        senseMessage=`Downloaded ${options.name}.JBI · ${count} touches (${pvars(result.base,count)}). Complete the two-person dry-run review before running it.`;
        if(result.archive==="failed")senseMessage+=" Server copy failed. Keep the downloaded file.";
        if(result.run==="failed")senseMessage+=" Run filing failed. Keep the downloaded file.";
      }catch(e){senseMessage=e.message;}
      finally{senseBusy=false;changed();}
    };
    const senseFile=get("wbSenseVarDatFile");
    get("wbSenseVarDatBtn").onclick=()=>{paint();if(senseReady()&&env.importVarDat)senseFile.click();};
    senseFile.onchange=async()=>{
      const file=senseFile.files&&senseFile.files[0];paint();
      if(!file||!senseReady()||!env.importVarDat){senseFile.value="";return;}
      const prior=accepted,base=surfaceRecord(),fit=get("wbSenseFit").value;
      const preservePlacement=(senseJob?.layout??get("wbSenseLayout").value)==="path";
      senseBusy=true;senseMessage="Importing sensed touches…";paint();
      try{
        const count=senseCount(senseJob?.count??get("wbSensePoints").value);
        const records=await env.importVarDat(await fileBody(file),44,count,"plate-sense");
        if(prior!==env.state().meta.params?.substrate_registration||pending()||env.busy()||env.guest?.())
          throw new Error("The surface changed during import. Review it, then import the sensed touches again.");
        env.pause();
        const ok=await env.apply(JSON.stringify({...base,version:3,sensed:{base:44,points:records,fit}}),{preservePlacement});
        if(!ok)throw new Error("Sensed touches were not applied. The previous registration is unchanged. Review the placement message below.");
        senseMessage=`Sensed touches imported from ${file.name} (${pvars(44,count)}). The part is seated on the sensed surface.`;
      }catch(e){senseMessage=e.message;}
      finally{senseBusy=false;senseFile.value="";changed();}
    };
    get("wbSenseClearBtn").onclick=async()=>{
      paint();if(!senseReady()||registration.version!==3)return;
      senseBusy=true;senseMessage="Removing sensed touches…";paint();
      try{
        env.pause();
        if(!await env.apply(JSON.stringify(surfaceRecord())))
          throw new Error("Sensed touches were not removed. The previous registration is unchanged. Review the placement message below.");
        senseMessage="Sensed touches removed. The four-corner surface is active.";
      }catch(e){senseMessage=e.message;}
      finally{senseBusy=false;changed();}
    };
    paint();
    try{
      const saved=JSON.parse(env.storage?.getItem(env.storageKey)||"{}");
      if(saved.senseRegistration===surfaceKey()){
        if(saved.senseJob?.base===44)senseJob={base:44,count:senseCount(saved.senseJob.count),layout:saved.senseJob.layout==="path"?"path":"grid"};
        for(const id of senseFields)if(saved.senseValues?.[id]!==undefined)get(id).value=saved.senseValues[id];
      }
      if(saved.pending && saved.gen===env.state().meta.gen){
        for(const id of fields)if(saved.values?.[id]!==undefined)get(id).value=saved.values[id];
        if(touch)touch.checked=!!saved.touch;
        syncTouch();
        dirty=true;message=saved.message||"";paint();
      }
    }catch(e){}
    paint();
    return {paint,apply,pending,error};
  }
  root.WaamSubstrate={create,record};
})(typeof module==="object"&&module.exports?module.exports:window);
