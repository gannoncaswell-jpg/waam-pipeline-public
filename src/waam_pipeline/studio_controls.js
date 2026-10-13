
function switchStep(step, focus=false){
  if(!["prepare","place","review","export"].includes(step)) step="prepare";
  if(!S.meta.workbench) step="review";
  if(UI.step==="place" && step!=="place") window.waamPlacement?.cancel();
  UI.step=step;
  for(const tab of document.querySelectorAll("[data-step]")){
    const selected=tab.dataset.step===step;
    tab.setAttribute("aria-selected",String(selected));
    tab.tabIndex=selected?0:-1;
    if(selected && focus) tab.focus();
  }
  for(const panel of document.querySelectorAll(".step-panel")) panel.hidden=panel.id!=="step-"+step;
  document.querySelector("aside").scrollTop=0;
  if(step!=="place") document.getElementById("movePart").checked=false;
  window.waamPlacement?.update();
  rememberView();
  draw();
}

function openStep(step, focus=false){
  window.waamWindow?.showInspector();
  switchStep(step,focus);
}
function rememberView(nextStep){
  if(nextStep) UI.step=nextStep;
  try{
    const flags={};
    for(const el of document.querySelectorAll(".layers-body input")) flags[el.id]=el.checked;
    sessionStorage.setItem(UI.storage,JSON.stringify({step:nextStep||UI.step,view:UI.view,title:S.meta.title,owner:UI.viewOwner||null,
      preferencesPending:!!UI.preferencesPending,
      ...camera(),panX:panX/(canvas.width||1),panY:panY/(canvas.height||1),
      cursor,style:renderStyle.value,speed:document.getElementById("speed").value,flags}));
  }catch(e){                                                              }
}
function camera(){
  return {center:[cx,cy,cz],span,yaw,pitch,zoom,panX,panY,perspective,dolly};
}
function restoreCamera(v){
  [cx,cy,cz]=v.center;
  ({span,yaw,pitch,zoom,panX,panY}=v);
  perspective=v.perspective===true;
  dolly=Number.isFinite(v.dolly)?v.dolly:0;
  updateProjectionControl();rememberView();
}
function panCamera(dx,dy){
  let scale=cameraScale();
  if(perspective){
    const eyeDist=cameraEyeDistance();

    scale*=eyeDist/Math.max(Math.abs(eyeDist-dolly),eyeDist*.02);
  }
  const sy=Math.sin(yaw),cyaw=Math.cos(yaw),sp=Math.sin(pitch),cp=Math.cos(pitch);
  cx+=(-cyaw*dx+sy*sp*dy)/scale;
  cy+=(sy*dx+cyaw*sp*dy)/scale;
  cz+=cp*dy/scale;
}
function updateProjectionControl(){
  const button=document.getElementById("projectionToggle");
  button.textContent=perspective?"Persp":"Ortho";
  button.setAttribute("aria-pressed",String(perspective));
  button.title=perspective?"Switch to orthographic projection":"Switch to perspective projection for free travel";
  document.getElementById("hud").textContent=(gpu && gpu.active?"":"Basic preview · ")+
    "Drag to orbit · Shift / middle-drag to pan · "+
    (perspective?"Scroll to travel · Shift-scroll for fine travel":"Scroll to zoom");
}
function togglePerspective(){
  if(UI.busy) return;
  window.waamPlacement?.cancel();drag=null;
  perspective=!perspective;
  updateProjectionControl();draw();rememberView();
}
function frameView(view){
  window.waamPlacement?.cancel();
  UI.view=view;
  panX=panY=dolly=0;
  zoom=view==="part"?partZoom:1.45;
  [cx,cy,cz]=view==="part"?[pcx,pcy,pcz]:cellCenter;
  document.getElementById("cellView").setAttribute("aria-pressed",String(view==="cell"));
  document.getElementById("partView").setAttribute("aria-pressed",String(view==="part"));
  draw();rememberView();
}
function setBusy(message){
  UI.busy=!!message;
  document.getElementById("busy").hidden=!message;
  document.getElementById("wb").setAttribute("aria-busy",String(!!message));
  for(const el of document.querySelectorAll("#cellView, #partView, #fitView, #showCompleted, #projectionToggle, [data-camera]")) el.disabled=!!message;
  if(message){
    setPlaying(false);
    document.getElementById("busyText").textContent=message;
  }
  window.waamPlacement?.update();
  window.waamLibrary?.update();
  window.waamHelp?.refresh();
}

function standardView(name){
  if(UI.busy) return;
  window.waamPlacement?.cancel();
  const views={iso:[-.85,.60],top:[0,Math.PI/2],front:[0,0],right:[-Math.PI/2,0]};
  const next=views[name];if(!next) return;
  yaw=next[0];pitch=next[1];frameView("part");
}
function revealField(field){
  const panel=field.closest(".step-panel");
  if(panel) openStep(panel.id.replace("step-",""));
  const detail=field.closest("details");if(detail) detail.open=true;
  field.focus();
  field.reportValidity();
}


function sourceSelection(name,pending=false){
  const loaded=name!==null;
  document.getElementById("stlUnitsRow").hidden=!loaded;
  const info=document.getElementById("sourceInfo");
  if(pending) info.textContent=name+" · ready to slice";
  else if(loaded && S.meta.stl){
    const dims=S.meta.stl.size_mm.map(v=>Number(v).toFixed(1)).join(" × ");
    const units={mm:"millimetres",in:"inches",m:"metres"}[S.meta.stl.resolved_units]||S.meta.stl.resolved_units;
    info.textContent=name+" · "+dims+" mm ("+units+")";
  }else if(loaded) info.textContent=name+" · loaded STL";
  else info.textContent="Demo selected. Slice to update the preview.";
}
function updateCorrectionControls(){
  const crashes=S.violations.filter(v=>v.blocking);
  const beadOnly=crashes.length>0 && crashes.every(v=>/^bead(?:\b|[_#:\d])/i.test(String(v.obstacle)));
  const correct=document.getElementById("correctBtn");
  const preview=S.meta.cell_profile && S.meta.cell_profile.planning_only;
  correct.disabled=UI.busy || beadOnly || !!preview || !!S.meta.substrate;
  correct.title=S.meta.substrate?"Move the bead within the measured substrate using Place.":preview?S.meta.cell_profile.export_reason:
    beadOnly?"Change the part geometry or slice settings to resolve deposited-bead collisions.":"";
}
function substrateRegistrationError(){
  return window.waamSubstrate?.error() || (S.meta.registration_pending?
    "Substrate registration is pending. Register the corner entries or clear registration before saving or exporting.":"");
}
function buildDraftError(){return window.waamDraft?.error()||"";}
function renderClearance(){
  const registrationError=substrateRegistrationError() || buildDraftError();
  const checked=S.meta.clearance_checked!==false;
  const crashes=S.violations.filter(v=>v.blocking);
  updateCorrectionControls();
  const proximity=S.violations.length-crashes.length;
  const state=registrationError?"pending":!checked?"pending":crashes.length?"blocked":"clear";
  const title=registrationError?(substrateRegistrationError()?"Substrate registration pending":"Changes need checking"):!checked?"Clearance not checked":crashes.length?crashes.length+" blocking findings":"Torch clearance passed";
  const detail=registrationError || (!checked?(S.meta.cell_profile?S.meta.cell_profile.export_reason:
    "Slice and check the build before exporting."):
    crashes.length?"Robot export is blocked. Inspect the findings and adjust the part or placement.":
    (proximity?proximity+" proximity findings, including permitted support contact. ":"")+"Review the toolpath before export. Final checks run during export.");
  const badge=document.getElementById("clearanceBadge");
  badge.dataset.state=state;badge.textContent=title;
  for(const id of ["reviewVerdict","exportVerdict"]){
    const panel=document.getElementById(id);panel.dataset.state=state;
    panel.querySelector("strong").textContent=title;
    panel.querySelector("p").textContent=detail;
  }
  const box=document.getElementById("viol");box.replaceChildren();
  if(buildDraftError()){
    const line=document.createElement("p");line.className="help";
    line.textContent="The preview and findings describe the last checked build. Check your current settings to update them.";
    box.appendChild(line);return;
  }
  if(!S.violations.length){
    const line=document.createElement("p");line.className=checked?"clean":"help";
    line.textContent=checked?"No findings at the required margin.":"Clearance has not been evaluated.";
    box.appendChild(line);return;
  }
  const groups=new Map();
  for(const finding of [...crashes,...S.violations.filter(v=>!v.blocking)]){
    const raw=String(finding.obstacle);
    const obstacle=raw==="bead_test_coupon"?"Test substrate":/^bead(?:\b|[_#:\d])/i.test(raw)?"Deposited bead":raw.replace(/_/g," ");
    const key=(finding.blocking?"blocked:":"near:")+obstacle;
    if(!groups.has(key)) groups.set(key,{obstacle,blocking:finding.blocking,items:[]});
    groups.get(key).items.push(finding);
  }
  let index=0;
  for(const group of groups.values()){
    const details=document.createElement("details");details.className="violation-group";details.open=index++===0;
    const summary=document.createElement("summary");
    summary.textContent=group.items.length+" × "+group.obstacle+(group.blocking?" · blocking":" · proximity");
    details.appendChild(summary);
    const hint=document.createElement("p");
    const worst=group.items.reduce((a,b)=>a.clearance<b.clearance?a:b);
    hint.textContent="Minimum clearance "+worst.clearance.toFixed(1)+" mm. "+
      (group.blocking?(group.obstacle==="Deposited bead"?"Change the part geometry or slice settings, then check again.":"Move the part away from this obstacle and check again."):"Inspect the location and confirm the modeled setup.");
    details.appendChild(hint);
    const moves=[...new Map([worst,...group.items].map(v=>[v.move,v])).values()];
    for(const finding of moves.slice(0,6)){
      const button=document.createElement("button");button.type="button";
      button.textContent="Inspect move "+finding.move+" · "+finding.clearance.toFixed(1)+" mm";
      button.onclick=()=>{setPlaying(false);cursor=Math.min(N-1,Math.max(0,finding.move));slider.value=cursor;frameView("part");};
      details.appendChild(button);
    }
    if(moves.length>6){const more=document.createElement("p");more.textContent=(moves.length-6)+" more affected moves in this group.";details.appendChild(more);}
    box.appendChild(details);
  }
}
function readyForRobotExport(){
  if(UI.busy) return false;
  if(buildDraftError()){
    document.getElementById("wbMsg").textContent=buildDraftError();return false;
  }
  if(substrateRegistrationError()){
    switchStep("place");document.getElementById("wbMsg").textContent=substrateRegistrationError();return false;
  }
  if(S.meta.cell_profile && S.meta.cell_profile.planning_only){
    switchStep("review");
    document.getElementById("wbMsg").textContent=S.meta.cell_profile.export_reason;
    return false;
  }
  if(S.meta.clearance_checked===false || S.violations.some(v=>v.blocking)){
    switchStep("review");
    document.getElementById("wbMsg").textContent="Resolve the clearance findings and check the build before exporting a robot job.";
    return false;
  }
  return true;
}
function initializeStudioUI(){
  const view=document.getElementById("view"), dpr=window.devicePixelRatio||1;
  canvas.width=view.clientWidth*dpr;canvas.height=view.clientHeight*dpr;
  let saved={};
  try{saved=JSON.parse(sessionStorage.getItem(UI.storage)||"{}");}catch(e){}
  if(!location.hash){
    if(Number.isFinite(saved.yaw)) yaw=saved.yaw;
    if(Number.isFinite(saved.pitch)) pitch=Math.max(-1.55,Math.min(1.55,saved.pitch));
    if(Number.isFinite(saved.zoom)) zoom=Math.max(.15,Math.min(200,saved.zoom));
    perspective=saved.perspective===true;
    dolly=saved.title===S.meta.title && Number.isFinite(saved.dolly)?saved.dolly:0;
    if(saved.title===S.meta.title){
      if(Array.isArray(saved.center) && saved.center.length===3 && saved.center.every(Number.isFinite))
        [cx,cy,cz]=saved.center;

      panCamera(Number.isFinite(saved.panX)?saved.panX*canvas.width:0,
                Number.isFinite(saved.panY)?saved.panY*canvas.height:0);
    }
    panX=panY=0;
    if(Number.isFinite(saved.cursor)) cursor=Math.max(0,Math.min(N-1,saved.cursor));
    slider.value=Math.floor(cursor);
  }
  UI.view=saved.view==="part"?"part":"cell";
  if(["solid","wire"].includes(saved.style)) renderStyle.value=saved.style;
  if(["60","180","600","2400"].includes(saved.speed)) document.getElementById("speed").value=saved.speed;
  for(const el of document.querySelectorAll(".layers-body input")){
    if(saved.flags && typeof saved.flags[el.id]==="boolean") el.checked=saved.flags[el.id];
    el.addEventListener("change",()=>rememberView());
  }
  document.getElementById("cellView").setAttribute("aria-pressed",String(UI.view==="cell"));
  document.getElementById("partView").setAttribute("aria-pressed",String(UI.view==="part"));
  document.getElementById("cellView").onclick=()=>frameView("cell");
  document.getElementById("partView").onclick=()=>frameView("part");
  document.getElementById("fitView").onclick=()=>frameView(UI.view);
  document.getElementById("showCompleted").onclick=()=>{
    if(UI.busy)return;
    if(buildDraftError()&&window.waamDraft?.blocked()===null){document.getElementById("wbMsg").textContent=buildDraftError();return;}
    setPlaying(false);cursor=Math.max(0,N-1);slider.value=cursor;frameView("part");
  };
  document.getElementById("projectionToggle").onclick=togglePerspective;
  updateProjectionControl();
  for(const button of document.querySelectorAll("[data-camera]")) button.onclick=()=>standardView(button.dataset.camera);
  for(const tab of document.querySelectorAll("[data-step]")){
    tab.onclick=()=>openStep(tab.dataset.step);
    if(!S.meta.workbench && tab.dataset.step!=="review") tab.disabled=true;
    tab.onkeydown=e=>{
      if(!["ArrowRight","ArrowLeft","Home","End"].includes(e.key)) return;
      e.preventDefault();
      const tabs=[...document.querySelectorAll('[data-step]:not(:disabled)')], at=tabs.indexOf(tab);
      const next=e.key==="Home"?0:e.key==="End"?tabs.length-1:(at+(e.key==="ArrowRight"?1:-1)+tabs.length)%tabs.length;
      openStep(tabs[next].dataset.step,true);
    };
  }
  for(const button of document.querySelectorAll("[data-next]")) button.onclick=()=>switchStep(button.dataset.next);
  for(const detail of document.querySelectorAll(".accordion")) detail.addEventListener("toggle",()=>{if(detail.open) draw();});
  for(const section of document.querySelectorAll(".workbench-only")) section.hidden=!S.meta.workbench;
  document.getElementById("clearanceBadge").onclick=()=>openStep("review");
  document.addEventListener("pointerdown",e=>{
    for(const id of ["layersMenu","layoutMenu"]){const menu=document.getElementById(id);if(menu && !menu.contains(e.target)) menu.open=false;}
  });
  window.addEventListener("pagehide",()=>rememberView());
  renderClearance();switchStep(saved.step||"prepare");
  if(saved.title!==S.meta.title){cursor=0;slider.value=0;frameView(UI.view);}
}
function resizeStudioView(){
  window.waamPlacement?.cancel();
  const view=document.getElementById("view"), dpr=window.devicePixelRatio||1;
  canvas.width=view.clientWidth*dpr;canvas.height=view.clientHeight*dpr;
  draw();rememberView();
}
