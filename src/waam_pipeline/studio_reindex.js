(function(root){
  "use strict";
  function create(env){
    const get=id=>env.document.getElementById(id);
    const inputs=["wbReindexProfileName","wbReindexProfileList","wbReindexClear","wbReindexTouchName","wbReindexJobName"];
    let profile={version:1,kind:"manpro-reindex",setup:null,clear_mm:50,reference:null,source_job:null};
    let newVarDat="",report=null,setupSummary=null,sourceSummary=null,busy=false,message="";
    const ready=()=>!busy&&!env.busy()&&!env.guest?.();
    const blocked=()=>env.blocked?.()||"";
    const writeReady=()=>ready()&&!blocked();
    const hasSetup=()=>!!profile.setup&&!!setupSummary;
    const canPreview=()=>hasSetup()&&!!profile.reference&&!!profile.source_job;
    const profileName=()=>get("wbReindexProfileName").value.trim()||"MANPRO";
    const jobName=(id,fallback)=>(get(id).value.trim()||fallback).toUpperCase().slice(0,32);
    const defaultJobName=name=>(name.slice(0,29)+"_RX").toUpperCase();
    const fixed=(value,digits=2)=>Number(value).toFixed(digits);
    const norm=values=>Math.hypot(...values);
    const calls=value=>value.map(call=>`${call.kind} ${call.job}`).join(", ")||"none";
    const archiveNote=result=>result.archive==="failed"?" Server copy failed. Keep the downloaded file.":"";
    function invalidate(){newVarDat="";report=null;}
    function clearSource(){profile={...profile,source_job:null};sourceSummary=null;get("wbReindexJobName").value="";}
    function clearValue(){
      const value=Number(get("wbReindexClear").value);
      if(!Number.isFinite(value)||value<20||value>200)throw new Error("Enter a clear height from 20 to 200 mm.");
      return value;
    }
    function pointTable(){
      const table=env.document.createElement("table"),caption=env.document.createElement("caption");
      caption.textContent="Robot points in the re-indexed copy";table.appendChild(caption);
      function row(parent,values,tag){
        const tr=env.document.createElement("tr");
        for(const value of values){
          const cell=env.document.createElement(tag);cell.textContent=value;
          if(tag==="th")cell.scope="col";
          tr.appendChild(cell);
        }
        parent.appendChild(tr);
      }
      const head=env.document.createElement("thead"),body=env.document.createElement("tbody");
      row(head,["Robot record","S1 (°)","Point move (mm)","Largest joint change (°)"],"th");
      for(const p of report.points)row(body,[String(p.record),fixed(p.station_deg),fixed(p.moved_mm),fixed(p.max_joint_change_deg,3)],"td");
      table.appendChild(head);table.appendChild(body);return table;
    }
    function paint(){
      const ok=ready(),writeOk=writeReady();
      const disable=(node,value)=>{if(node&&node.disabled!==value)node.disabled=value;};
      for(const id of inputs)disable(get(id),id==="wbReindexProfileList"?!ok:!writeOk);
      const actions={
        Refresh:!!env.get,Load:!!env.get&&!!get("wbReindexProfileList").value,
        Save:hasSetup()&&!!env.post,Download:hasSetup(),Upload:!!env.post,
        Setup:!!env.importVarDat&&!!env.post,Touch:hasSetup()&&!!env.download,
        Ref:hasSetup()&&!!env.post,Source:!!env.post,New:canPreview()&&!!env.post,
        Emit:canPreview()&&!!report&&!!newVarDat&&!!env.download
      };
      for(const [name,enabled] of Object.entries(actions)){
        const actionReady=name==="Refresh"?ok:writeOk;
        disable(get(`wbReindex${name}Btn`),!actionReady||!enabled);
        if(["Upload","Setup","Ref","Source","New"].includes(name))disable(get(`wbReindex${name}File`),!writeOk||!enabled);
      }
      get("reindexSetupReadout").textContent=setupSummary?
        `Touch depths: ${Object.values(setupSummary.depths_mm).map(v=>fixed(v)).join(", ")} mm; `+
        `face angle ${fixed(setupSummary.face_angle_deg)}°; clear height ${fixed(profile.clear_mm)} mm above the top (Z ${fixed(setupSummary.clear_z)} mm). `+
        (profile.reference?"Reference touches accepted.":"No reference touches accepted."):
        "No setup touches imported. Store T1, T2, T3, T4, S1, S2, E1, E2 in P040–P047.";
      get("reindexSourceReadout").textContent=sourceSummary?
        `${sourceSummary.name}: ${sourceSummary.points} points; ${sourceSummary.station_records} station records; job targets: ${calls(sourceSummary.calls)}. `+
        "CLEAN_R1 is cell-fixed. Any CALL or JUMP target taught on the block needs its own re-index.":"No class job uploaded.";
      let result="No new touches previewed.";
      if(report){
        const shift=report.shift,residuals=shift.residuals_mm;
        const worst=residuals.reduce((best,value,i)=>Math.abs(value)>Math.abs(residuals[best])?i:best,0);
        const move=Math.max(0,...report.points.map(p=>p.moved_mm));
        const joint=Math.max(0,...report.points.map(p=>p.max_joint_change_deg));
        result=`Shift at the block: ${fixed(norm(shift.center_shift_mm))} mm; rotation: ${fixed(norm(shift.rotation_deg),3)}°. `+
          `Worst residual: ${shift.labels[worst]} ${fixed(Math.abs(residuals[worst]),3)} mm; largest point move: ${fixed(move)} mm; largest joint change: ${fixed(joint,3)}°.\n`+
          `Residuals: ${residuals.map((r,i)=>`${shift.labels[i]} ${fixed(r,3)} mm`).join("; ")}.`;
      }
      get("reindexReport").textContent=result;
      if(report)get("reindexReport").appendChild(pointTable());
      get("reindexStatus").textContent=blocked()||message;
    }
    async function run(start,step,allowed=()=>true,writing=true){
      paint();if(!(writing?writeReady():ready())||!allowed())return;
      busy=true;message=start;paint();
      try{message=await step();}
      catch(e){message=e.message;}
      finally{busy=false;paint();env.changed?.();}
    }
    async function profiles(){
      const result=await env.get("/api/reindex/profiles"),select=get("wbReindexProfileList");
      const previous=select.value;
      select.textContent="";
      for(const entry of result.profiles){
        const option=env.document.createElement("option");
        option.value=entry.name;option.textContent=entry.name;select.appendChild(option);
      }
      select.value=result.profiles.some(p=>p.name===previous)?previous:(result.profiles[0]?.name||"");
      return result.profiles.length;
    }
    const refresh=()=>run("Refreshing saved profiles…",async()=>`${await profiles()} saved profiles.`,()=>!!env.get,false);
    async function acceptProfile(candidate){
      if(!candidate||candidate.version!==1||candidate.kind!=="manpro-reindex")
        throw new Error("Choose a version 1 ManPro re-index profile.");
      const setup=await env.post("/api/reindex/setup",{profile:candidate});
      const source=candidate.source_job?await env.post("/api/reindex/source",{text:candidate.source_job.text}):null;
      profile=candidate;setupSummary=setup.summary;sourceSummary=source?.summary||null;invalidate();
      get("wbReindexClear").value=String(profile.clear_mm);
      get("wbReindexJobName").value=sourceSummary?defaultJobName(sourceSummary.name):"";
    }
    function fileAction(name,start,allowed,step){
      const file=get(`wbReindex${name}File`),button=get(`wbReindex${name}Btn`);
      button.onclick=()=>{paint();if(!button.disabled)file.click();};
      file.onchange=()=>{
        const chosen=file.files&&file.files[0];
        if(!chosen){file.value="";return Promise.resolve();}
        if(blocked()){file.value="";paint();return Promise.resolve();}
        return run(start,async()=>{
          try{return await step(chosen);}
          finally{file.value="";}
        },allowed);
      };
    }
    get("wbReindexRefreshBtn").onclick=refresh;
    get("wbReindexLoadBtn").onclick=()=>run("Loading the profile…",async()=>{
      const name=get("wbReindexProfileList").value;
      const result=await env.get("/api/reindex/profile?"+new URLSearchParams({name}));
      await acceptProfile(result.profile);get("wbReindexProfileName").value=name;
      return `Loaded ${name}. Run the same touch job after a swap, then import the new P044–P051 touches.`;
    },()=>!!env.get&&!!get("wbReindexProfileList").value);
    get("wbReindexSaveBtn").onclick=()=>run("Saving the profile…",async()=>{
      const result=await env.post("/api/reindex/profile?"+new URLSearchParams({name:profileName()}),{profile});
      await profiles();get("wbReindexProfileList").value=result.name;
      return `Saved ${result.name}. Download a local copy of the profile too.`;
    },hasSetup);
    get("wbReindexDownloadBtn").onclick=()=>run("Downloading the profile…",async()=>{
      const name=profileName()+".reindex.json";
      const blob=new Blob([JSON.stringify(profile,null,2)],{type:"application/json"});
      const url=URL.createObjectURL(blob),a=env.document.createElement("a");
      try{a.href=url;a.download=name;env.document.body.appendChild(a);a.click();}
      finally{a.remove();URL.revokeObjectURL(url);}
      return `Downloaded ${name}.`;
    },hasSetup);
    fileAction("Upload","Importing the profile…",()=>!!env.post,async chosen=>{
      await acceptProfile(JSON.parse(await chosen.text()));
      return `Imported ${chosen.name}.`;
    });
    fileAction("Setup","Checking setup touches…",()=>!!env.importVarDat&&!!env.post,async chosen=>{
      const records=await env.importVarDat(await chosen.text(),40,8);
      const candidate={...profile,setup:{records},clear_mm:clearValue(),reference:null,source_job:null};
      const result=await env.post("/api/reindex/setup",{profile:candidate});
      profile=candidate;setupSummary=result.summary;clearSource();invalidate();
      return "Setup touches imported (P040–P047). Reference cleared: touches from an old plan are not comparable. Download the touch job and record a new reference. Upload the class job exported with the block in THIS position.";
    });
    get("wbReindexClear").onchange=()=>run("Checking the clear height…",async()=>{
      const prior=profile.clear_mm;
      if(Number(get("wbReindexClear").value)===prior)return message;
      profile={...profile,reference:null};invalidate();
      const note="Reference cleared: touches from an old plan are not comparable.";
      try{
        const candidate={...profile,clear_mm:clearValue()};
        const result=profile.setup?await env.post("/api/reindex/setup",{profile:candidate}):null;
        profile=candidate;setupSummary=result?.summary||null;
      }catch(e){get("wbReindexClear").value=String(prior);throw new Error(`${e.message} ${note}`);}
      return `Clear height changed. ${note} Download the touch job and record a new reference.`;
    });
    get("wbReindexTouchBtn").onclick=()=>run("Preparing the touch job…",async()=>{
      const name=jobName("wbReindexTouchName","REIDX_TOUCH");
      const result=await env.download("/api/reindex/touch-job?"+new URLSearchParams({name,folder:env.jobFolder()}),{profile},name+".JBI");
      return `Downloaded ${name}.JBI. S1 at 0.0°, TOOL 0, CLEAN_R1; complete a two-person dry run (arc off, slow, hand on the E-stop). Results land in P044–P051.${archiveNote(result)}`;
    },hasSetup);
    fileAction("Ref","Checking reference touches…",hasSetup,async chosen=>{
      const result=await env.post("/api/reindex/touches",{profile,var_dat:await chosen.text()});
      profile={...profile,reference:{records:result.records}};clearSource();invalidate();
      return "Reference touches accepted (P044–P051). Upload the class job exported with the block in THIS position. Then save the profile and download a local copy.";
    });
    fileAction("Source","Checking the class job…",()=>!!env.post,async chosen=>{
      const text=await chosen.text(),result=await env.post("/api/reindex/source",{text});
      profile={...profile,source_job:{name:result.summary.name,text}};sourceSummary=result.summary;invalidate();
      get("wbReindexJobName").value=defaultJobName(sourceSummary.name);
      return `Class job ${sourceSummary.name} accepted. The reference must have been recorded with the block where this job was taught. Save the profile and download a local copy.`;
    });
    fileAction("New","Previewing the block shift…",canPreview,async chosen=>{
      invalidate();
      const text=await chosen.text(),result=await env.post("/api/reindex/preview",{profile,var_dat:text});
      report=result.report;newVarDat=text;
      return "Preview ready. Review the shift, rotation, residuals and point moves before downloading the re-indexed copy.";
    });
    get("wbReindexEmitBtn").onclick=()=>run("Preparing the re-indexed job…",async()=>{
      const name=jobName("wbReindexJobName",defaultJobName(profile.source_job.name));
      const result=await env.download("/api/reindex/emit?"+new URLSearchParams({name,folder:env.jobFolder()}),{profile,var_dat:newVarDat},name+".JBI");
      return "Load it BESIDE the class job; two-person review and an arc-off dry run before the class welds with it."+archiveNote(result);
    },()=>canPreview()&&!!report&&!!newVarDat);
    get("wbReindexProfileList").onchange=()=>paint();
    paint();
    return {paint,refresh};
  }
  root.WaamReindex={create};
})(typeof module==="object"&&module.exports?module.exports:window);
