(function(root){
  "use strict";


  function create(env){
    const get=id=>env.document.getElementById(id);
    const fileBody=file=>file.arrayBuffer?file.arrayBuffer():file.text();
    const inputs=["wbSenseLayer","wbLayerSpacing","wbLayerSenseName"];
    const BASE=120,FLAG_BASE=80;
    let busy=false,message="";
    function sets(){
      try{const value=JSON.parse(get("wbLayerSense").value||"[]");return Array.isArray(value)?value:[];}
      catch(e){return [];}
    }
    function ready(){
      const state=env.state();
      return state.meta.params?.cell_profile==="lc50s"&&state.meta.clearance_checked===true&&
        !env.busy()&&!busy&&!env.guest?.()&&!env.draftError?.();
    }
    const mm=value=>Number.isFinite(value)?`${value>=0?"+":""}${value.toFixed(2)} mm`:"—";
    const pvars=(base,count)=>`P${String(base).padStart(3,"0")}–P${String(base+count-1).padStart(3,"0")}`;
    function paint(){
      const ok=ready(),done=env.state().meta.layer_sense||[];
      for(const id of inputs)get(id).disabled=!ok;
      get("wbLayerSenseJobBtn").disabled=!ok||!env.layerSenseJob;
      get("wbLayerVarDatBtn").disabled=get("wbLayerVarDatFile").disabled=!ok||!env.importVarDat||!env.layerSensePlan;
      get("wbLayerSenseClearBtn").disabled=!ok||!sets().length;
      get("layerSenseReadout").textContent=done.length?done.map(m=>
        `Layer ${m.layer}: ${m.touched??m.points} of ${m.planned??m.points} touched`+
        (m.missed?.length?` (missed ${m.missed.join(", ")})`:"")+`, top ${mm(m.mean_error_mm)} on average (${mm(m.error_range_mm[0])} to ${mm(m.error_range_mm[1])}), largest at ${m.worst}.`+
        (m.warnings?.length?` Warning: ${m.warnings.join(" ")}`:"")).join(" "):
        "No layer touched. Every layer is seated on the model.";
      get("layerSenseStatus").textContent=env.draftError?.()||message;
    }
    function options(){
      const layer=Number(get("wbSenseLayer").value),spacing=Number(get("wbLayerSpacing").value);
      if(!Number.isInteger(layer)||layer<0)throw new Error("Enter the layer to touch as a whole number from 0.");
      if(!Number.isFinite(spacing))throw new Error("Enter the touch spacing in mm.");
      return {layer,spacing,name:get("wbLayerSenseName").value.trim()||"LAYER_TOUCH"};
    }
    async function run(start,step){
      paint();if(!ready())return;
      busy=true;message=start;paint();
      try{message=await step();}
      catch(e){message=e.message;}
      finally{busy=false;paint();env.changed?.();}
    }
    get("wbLayerSenseJobBtn").onclick=()=>run("Preparing the layer touch job…",async()=>{
      const o=options(),result=await env.layerSenseJob(o);
      if(result.base!==BASE||result.flag_base!==FLAG_BASE||result.layer!==o.layer)
        throw new Error("The touch job did not match the requested layer and registers.");
      const note=(result.archive==="failed"?" Server copy failed. Keep the downloaded file.":"")+
        (result.run==="failed"?" Run filing failed. Keep the downloaded file.":"");
      return `Downloaded ${o.name.toUpperCase()}.JBI · ${result.count} touches on layer ${o.layer} (${pvars(BASE,result.count)}). `+
        `The job keeps going past a miss; 3 misses in a row stop it. Print through layer ${o.layer} first, and complete the two-person dry-run review before running it.${note}`;
    });
    const file=get("wbLayerVarDatFile");
    get("wbLayerVarDatBtn").onclick=()=>{paint();if(ready())file.click();};
    file.onchange=()=>{
      const chosen=file.files&&file.files[0];
      if(!chosen){file.value="";return Promise.resolve();}
      if(!ready()){file.value="";paint();return Promise.resolve();}
      return run("Importing layer touches…",async()=>{
        try{
          const o=options(),prior=sets(),newest=prior[prior.length-1];
          if(newest&&o.layer<=newest.layer)throw new Error(`Touch sets must go up the part: the last one is layer ${newest.layer}.`);
          const plan=await env.layerSensePlan(o);
          if(plan.base!==BASE||plan.flag_base!==FLAG_BASE)throw new Error("The touch plan did not match the expected registers.");
          const read=await env.importVarDat(await fileBody(chosen),plan.base,plan.count,"layer-touch",plan.flag_base);
          env.pause?.();
          const next=[...prior,{layer:o.layer,spacing:o.spacing,points:read.records,
            base:plan.base,flag_base:plan.flag_base,hits:read.hits}];
          if(!await env.apply(JSON.stringify(next)))
            throw new Error("Layer touches were not applied. The previous build is unchanged. Review the placement message below.");
          const touched=read.hits.filter(Boolean).length;
          return `Layer ${o.layer} touches imported from ${chosen.name} (${pvars(BASE,plan.count)}): ${touched} of ${plan.count} touched. `+
            "Every later layer is seated on the measured top; misses are left out.";
        }finally{file.value="";}
      });
    };
    get("wbLayerSenseClearBtn").onclick=()=>run("Removing the last layer touch set…",async()=>{
      const prior=sets(),removed=prior[prior.length-1];
      if(!removed)return "";
      const rest=prior.slice(0,-1);
      if(!await env.apply(rest.length?JSON.stringify(rest):""))
        throw new Error("The touch set was not removed. The previous build is unchanged.");
      return `Removed the layer ${removed.layer} touch set.`;
    });
    for(const id of inputs)get(id).addEventListener("input",()=>{message="";paint();});
    paint();
    return {paint,sets};
  }
  root.WaamLayerSense={create};
})(typeof module==="object"&&module.exports?module.exports:window);
