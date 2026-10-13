
(function(root){
  "use strict";
  function steps(state, guest=false){
    const hc=state.meta.params?.cell_profile==="hc10";
    const slice=hc?"Slice & preview":"Slice & check";
    return [
      {title:"Choose the cell", step:"prepare", target:"wbCellProfile", lines:[
        "In Prepare, select the cell you are planning to use.",
        "Confirm the equipment shown in Cell view."
      ]},
      {title:"Load a part", step:"prepare", target:"stlFile", lines:[
        "Choose a demo or a part from your library, or select Upload STL.",
        "Compare the displayed dimensions with your CAD model. Change STL units if needed."
      ]},
      {title:"Configure slicing", step:"prepare", target:"wbSlicer", lines:[
        "Bead height is the height each layer adds — about 2/3 of a single bead's measured height; use a bead preset or measure the wall's layer pitch. Enter the measured bead width and the travel speed. Bead-on-plate takes a bead count and spacing; the wall takes a length, layer count and thickness.",
        "Use Built-in for thin-wall contours, or ORNL for contours and infill in a closed solid STL.",
        "Select "+slice+" to generate the toolpath."
      ]},
      {title:"Position the part", step:"place", target:"wbOx", lines:[
        "In Place, drag Move or Rotate handles, or enter X and Y offsets and Z rotation.",
        hc?"Set the substrate thickness and lift-off gap to match the setup.":
          "To seat the part on a measured plate, open Measured surface setup, enter the four corner pulse records and contact-tip gaps, then calculate the surface. X follows corners 1 to 2. Otherwise set the substrate thickness and lift-off gap.",
        hc?"Select Apply placement. The part is positioned on the nominal table.":
          "Select Apply placement & check to update the clearance result."
      ]},
      {title:"Review the build", step:"review", target:"reviewVerdict", lines:[
        "In Review, play or scrub the timeline to inspect each layer and travel move.",
        hc?"The HC10DTP arm follows the toolpath using nominal cell geometry. Clearance is not checked.":
          "Select clearance findings to inspect their locations. Resolve blocking findings before exporting.",
        "Use Layers to show or hide the part, tooling, and collision volumes."
      ]},
      {title:"Save or export", step:"export", target:"wbJobName", lines:[
        guest?"Sign in to save builds and export files.":
          "In Export, name and save the build to keep its source and settings.",
        hc?"HC10DTP robot exports are unavailable until the cell, tool, and controller profiles are calibrated.":
          "For a JBI job, enter a controller job name, the ASF number from the pendant, the interpass dwell (up to 3600 s) and how many layers run between dwells. Optionally pause for a temperature reading every N layers, or wait between the beads of a thick wall. Confirm the physical platter setup when prompted.",
        hc?"You can save the build or download its G-code for further planning.":
          "ROS 2 export provides arc-off motion. Downloaded G-code requires conversion before robot use."
      ]}
    ];
  }
  function create(env){
    const doc=env.document, el=id=>doc.getElementById(id);
    const dialog=el("helpDlg"), trigger=el("helpBtn");
    const tabs=Array.from(doc.querySelectorAll("[data-help-topic]"));
    let index=0, topic="start", previousFocus=null, restoreFocus=true;
    function refresh(){
      const state=env.state(), guide=steps(state,!!env.guest());
      index=Math.max(0,Math.min(index,guide.length-1));
      const current=guide[index];
      el("helpProgress").textContent="Step "+(index+1)+" of "+guide.length;
      el("helpStepTitle").textContent=current.title;
      el("helpSteps").replaceChildren();
      for(const text of current.lines){
        const item=doc.createElement("li");item.textContent=text;el("helpSteps").appendChild(item);
      }
      const hc=state.meta.params?.cell_profile==="hc10";
      el("helpContext").textContent=!state.meta.workbench?
        "This file is a saved preview. Open the workbench to upload, slice, or save a build.":
        hc?"HC10DTP supports motion preview. Clearance checks and robot exports are unavailable.":
        "Use measured process settings. A preview or torch clearance result does not qualify a weld.";
      el("helpBack").disabled=index===0;
      el("helpNext").textContent=index===guide.length-1?"Done":"Next";
      el("helpGo").textContent="Open "+current.step[0].toUpperCase()+current.step.slice(1);
      el("helpGo").disabled=!!env.busy() || (!state.meta.workbench && current.step!=="review");
      el("helpGo").title=env.busy()?"Wait for the current update to finish.":"";
      el("helpWalkActions").hidden=topic!=="start";
    }
    function chooseTopic(value, focus=false){
      if(!["start","controls","troubleshoot"].includes(value))return;
      topic=value;
      for(const tab of tabs){
        const selected=tab.dataset.helpTopic===topic;
        tab.setAttribute("aria-selected",String(selected));tab.tabIndex=selected?0:-1;
        el("help-"+tab.dataset.helpTopic).hidden=!selected;
        if(selected && focus)tab.focus();
      }
      refresh();
    }
    function open(){
      if(dialog.open)return;
      previousFocus=doc.activeElement;
      restoreFocus=true;refresh();env.pause();
      dialog.showModal();el("helpClose").focus();
    }
    function close(restore=true){restoreFocus=restore;dialog.close();}
    function next(){
      if(index===steps(env.state()).length-1){close();return;}
      index++;refresh();el("helpStepTitle").focus();
    }
    function back(){index=Math.max(0,index-1);refresh();el("helpStepTitle").focus();}
    function go(){
      refresh();if(el("helpGo").disabled)return;
      const current=steps(env.state(),!!env.guest())[index];
      close(false);env.showStep(current.step);
      const target=el(current.target);
      if(target){
        if(current.target==="reviewVerdict")target.setAttribute("tabindex","-1");
        target.scrollIntoView({block:"center"});target.focus({preventScroll:true});
      }
    }
    trigger.onclick=open;el("helpClose").onclick=()=>close();
    el("helpNext").onclick=next;el("helpBack").onclick=back;el("helpGo").onclick=go;
    dialog.addEventListener("close",()=>{
      if(restoreFocus)(previousFocus?.isConnected?previousFocus:trigger).focus();
      restoreFocus=true;
    });

    dialog.addEventListener("keydown",event=>event.stopPropagation());
    for(const tab of tabs){
      tab.onclick=()=>chooseTopic(tab.dataset.helpTopic);
      tab.onkeydown=event=>{
        if(!["ArrowLeft","ArrowRight","Home","End"].includes(event.key))return;
        event.preventDefault();
        const at=tabs.indexOf(tab), length=tabs.length;
        const n=event.key==="Home"?0:event.key==="End"?length-1:
          (at+(event.key==="ArrowRight"?1:-1)+length)%length;
        chooseTopic(tabs[n].dataset.helpTopic,true);
      };
    }
    return {open,close,refresh,next,back,go,chooseTopic};
  }
  const api={create,steps};
  if(typeof module==="object" && module.exports)module.exports=api;
  root.WaamHelp=api;
})(typeof window==="undefined"?globalThis:window);
