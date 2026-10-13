(function(root){
  "use strict";
  function create(env){
    const get=id=>env.document.getElementById(id);
    const controls=["wbRunAddBtn","wbRunFiles","wbRunTemperature","wbRunUnit","wbRunStage",
      "wbRunTemperatureNote","wbRunTemperatureBtn","wbRunNote","wbRunNoteBtn","wbRunOutcome","wbRunDataClass",
      "wbRunHeight","wbRunWidth","wbRunNotes","wbRunArcmon","wbRunWaiver","wbRunFinishBtn"];
    let current=null,recent=[],loaded=false,busy=false,message="",refreshVersion=0;
    const available=()=>loaded&&!busy&&!env.busy()&&!env.guest()&&!env.blocked?.();
    const hasArcmon=()=>!!current?.arc_monitor?.files?.length;
    const canFinish=()=>available()&&!!current&&(hasArcmon()||get("wbRunWaiver").checked||get("wbRunOutcome").value==="not-run");
    function resetInputs(){
      for(const id of ["wbRunName","wbRunHeight","wbRunWidth","wbRunNotes","wbRunTemperature","wbRunTemperatureNote","wbRunNote"])get(id).value="";
      get("wbRunWaiver").checked=false;get("wbRunOutcome").value="completed";get("wbRunDataClass").value="preliminary";
    }
    function time(value,short=false){
      const date=new Date(value);
      return Number.isNaN(date.getTime())?String(value):short?
        date.toLocaleTimeString([],{hour:"2-digit",minute:"2-digit",hour12:false}):date.toLocaleString();
    }
    function paint(){
      const guest=env.guest(),ready=available();
      for(const id of controls)get(id).disabled=!ready||!current;
      get("wbRunName").disabled=!ready||!!current;
      get("wbRunFinishBtn").disabled=!canFinish();
      get("wbRunTemperature").max=get("wbRunUnit").value==="F"?"2500":"1400";
      get("runStatus").textContent=guest?"Sign in to keep a run log.":current?
        `Run ${current.run} · open since ${time(current.started_at,true)} · ${current.files.length} files`:
        loaded?"No run open. The next robot job or plate VAR.DAT starts one.":"Loading run log…";
      get("runMessage").textContent=guest?"":env.blocked?.()||message;
      get("runArcmonStatus").textContent=current?
        `${current.arc_monitor.files.length} arc monitor file${current.arc_monitor.files.length===1?"":"s"} in this run.`:"";
      paintPasses();
      const list=get("runRecent");list.textContent="";
      if(!guest){
        for(const run of recent){
          const row=env.document.createElement("p");row.className="help";
          row.textContent=`${run.run} · ${run.outcome} · `+(run.ack?`In OneDrive since ${time(run.ack.at)}`:"Waiting for the host copy");
          list.appendChild(row);
        }
        if(loaded&&!recent.length)list.textContent="No finished runs yet.";
      }
    }

    function paintPasses(){
      const box=get("runArcmonPasses");box.textContent="";
      const joined=[...(current?.arc_monitor?.summaries||[])].reverse().find(s=>s?.passes)?.passes;
      if(!joined)return;
      const line=text=>{const row=env.document.createElement("p");row.className="help";row.textContent=text;box.appendChild(row);};
      if(joined.error){line("Arc monitor not joined to passes: "+joined.error);return;}
      const num=(v,d)=>v===null||v===undefined?"not available":Number(v).toFixed(d);
      line(`Per pass (${joined.mode==="step"?"by step number":"in order"}) · ${joined.file}`);
      for(const w of joined.warnings||[])line("Warning: "+w);
      const rows=joined.rows||[];
      for(const r of rows.slice(0,40)){
        const welding=r.current_mean_a===null||r.current_mean_a===undefined
          ?"Welding: not available"
          :`Welding: ${num(r.current_mean_a,0)} A · ${num(r.voltage_mean_v,1)} V · `+
           `${num(r.weld_s,1)} s · ${num(r.energy_kj_mm,3)} kJ/mm`;
        const arc=`Total arc: ${num(r.arc_current_mean_a,0)} A · ${num(r.arc_voltage_mean_v,1)} V · `+
                  `${num(r.arc_on_s,1)} s`;
        line(`Pass ${r.pass}${r.asf?" · ASF#"+r.asf:""} · ${welding} · ${arc}`+
             (r.flags?" · "+r.flags:""));
      }
      const total=joined.rows_total||rows.length;
      if(total>40)line(`${total-40} more passes in ${joined.file}.`);
    }
    async function json(path,options){
      const response=await env.request(path,options),data=await response.json().catch(()=>null);
      if(!response.ok||!data?.ok)throw new Error(data?.error||`Run request failed (HTTP ${response.status}).`);
      return data;
    }
    async function refresh(){
      const version=++refreshVersion;
      if(env.guest()){current=null;recent=[];loaded=true;paint();return;}
      try{
        const data=await json("/api/run");
        if(version!==refreshVersion)return;
        if(current?.run!==data.active?.run)resetInputs();
        current=data.active;recent=data.recent;loaded=true;
        if(current)get("wbRunName").value=current.name||"";
      }catch(e){if(version!==refreshVersion)return;loaded=false;message="Run log unavailable: "+e.message;}
      paint();
    }
    function name(){
      const value=get("wbRunName").value.trim();
      if(!/^[A-Za-z0-9 _-]{0,40}$/.test(value))throw new Error("Run name: use at most 40 letters, digits, spaces, underscores or hyphens.");
      return value;
    }
    function response(result){
      const filed=result.headers.get("X-Waam-Run");
      message=filed==="failed"?"Run filing failed. Keep the downloaded or imported file.":"";
      paint();
      void refresh();
      return filed;
    }
    const post=(path,body)=>json(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    async function perform(start,step){
      if(!available()||!current)return;
      if(!env.begin(start))return;
      busy=true;message=start;paint();
      try{message=await step();}
      catch(e){message=e.message;}
      finally{await refresh();busy=false;env.end();paint();}
    }
    async function upload(input,kind){
      const files=Array.from(input.files||[]);
      if(!files.length)return;
      try{
        await perform("Adding files to the run…",async()=>{
          const failures=[];let count=0;
          for(const file of files){
            try{
              await json("/api/run/add?"+env.query({kind,name:file.name}),{method:"POST",body:await file.arrayBuffer()});
              count++;
            }catch(e){failures.push(`${file.name}: ${e.message}`);}
          }
          return `Added ${count} file${count===1?"":"s"}.`+(failures.length?" "+failures.join(" "):"");
        });
      }finally{input.value="";}
    }
    get("wbRunAddBtn").onclick=()=>{if(available()&&current)get("wbRunFiles").click();};
    get("wbRunFiles").onchange=()=>upload(get("wbRunFiles"),"attachment");
    get("wbRunArcmon").onchange=()=>upload(get("wbRunArcmon"),"arcmon");
    get("wbRunTemperatureBtn").onclick=()=>perform("Recording temperature…",async()=>{
      const field=get("wbRunTemperature"),value=Number(field.value),unit=get("wbRunUnit").value;
      if(!field.value.trim()||!Number.isFinite(value)||value< -40||value>(unit==="F"?2500:1400))
        throw new Error(`Enter a temperature from -40 to ${unit==="F"?2500:1400} °${unit}.`);
      await post("/api/run/temperature",{value,unit,stage:get("wbRunStage").value,note:get("wbRunTemperatureNote").value.trim()});
      field.value="";get("wbRunTemperatureNote").value="";
      return `Recorded ${value} °${unit}.`;
    });
    get("wbRunNoteBtn").onclick=()=>perform("Adding note…",async()=>{
      const field=get("wbRunNote"),text=field.value.trim();
      if(!text)throw new Error("Type a note to add.");
      await post("/api/run/note",{text});
      field.value="";
      return "Note added to the run.";
    });
    get("wbRunFinishBtn").onclick=()=>{
      if(!canFinish())return;
      const id=current.run;
      if(!env.confirm(`Finish and seal run ${id}? Files can no longer be added.`))return;
      return perform("Finishing and sealing the run…",async()=>{
        const measurement=id=>{
          const value=get(id).value.trim();
          if(!value)return null;
          if(!Number.isFinite(Number(value))||Number(value)<0)throw new Error("Measurements must be nonnegative numbers in mm.");
          return Number(value);
        };
        await post("/api/run/finish",{outcome:get("wbRunOutcome").value,data_class:get("wbRunDataClass").value,
          height_mm:measurement("wbRunHeight"),width_mm:measurement("wbRunWidth"),notes:get("wbRunNotes").value.trim(),
          arcmon_waiver:get("wbRunWaiver").checked});
        resetInputs();
        return `Run ${id} finished and sealed.`;
      });
    };
    for(const id of ["wbRunOutcome","wbRunWaiver","wbRunUnit"])get(id).addEventListener("change",paint);
    get("wbRunName").addEventListener("input",()=>env.changed());
    get("runPanel").addEventListener("toggle",()=>{if(get("runPanel").open&&!busy&&!env.busy())void refresh();});
    paint();
    return {paint,refresh,name,response};
  }
  root.WaamRuns={create};
})(typeof module==="object"&&module.exports?module.exports:window);
