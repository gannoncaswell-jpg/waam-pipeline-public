


(function(root){
  "use strict";
  const KEY="waamStudioLayout:v1";


  const WIDTH=Object.freeze({min:280,max:760,step:20,viewportShare:.6});
  const SCALES=Object.freeze([.9,1,1.15,1.3]);
  const DEFAULTS=Object.freeze({width:null,scale:1,inspector:true});

  function clampWidth(value){
    if(typeof value!=="number" || !Number.isFinite(value))return null;
    return Math.round(Math.min(WIDTH.max,Math.max(WIDTH.min,value)));
  }
  function normalize(value){
    const source=value && typeof value==="object" && !Array.isArray(value)?value:{};
    return {
      width:clampWidth(source.width),
      scale:SCALES.includes(source.scale)?source.scale:DEFAULTS.scale,
      inspector:typeof source.inspector==="boolean"?source.inspector:DEFAULTS.inspector,
    };
  }
  function create(env){
    const doc=env.document, el=id=>doc.getElementById(id), body=doc.body;
    const aside=doc.querySelector("aside"), splitter=el("sideSplitter");
    const widthInput=el("layoutWidth"), widthOut=el("layoutWidthValue"), scaleInput=el("layoutScale");
    const toggles=Array.from(doc.querySelectorAll("[data-inspector-toggle]"));
    const storage=()=>env.storage!==undefined?env.storage:root.localStorage;
    const viewport=()=>env.viewport?.() ?? root.innerWidth ?? 1920;
    let state={...DEFAULTS}, drag=null;
    try{state=normalize(JSON.parse(storage()?.getItem(KEY)||"null"));}catch(_){state={...DEFAULTS};}

    function maxWidth(){return Math.max(WIDTH.min,Math.min(WIDTH.max,Math.floor(viewport()*WIDTH.viewportShare)));}
    function measured(){
      const width=aside && !aside.hidden?aside.getBoundingClientRect?.().width:0;
      return width>0?Math.round(width):null;
    }
    function current(){return Math.min(maxWidth(),state.width ?? measured() ?? 350);}
    function save(){
      try{storage()?.setItem(KEY,JSON.stringify(state));}catch(_){                     }
    }
    function paint(){
      const width=current();
      if(state.width===null){body.style.removeProperty("--side-w");delete body.dataset.sideWidth;}
      else{body.style.setProperty("--side-w",state.width+"px");body.dataset.sideWidth="custom";}
      body.style.setProperty("--wb-scale",String(state.scale));
      if(state.scale===1)delete body.dataset.wbScaled;else body.dataset.wbScaled="true";
      body.dataset.inspector=state.inspector?"shown":"hidden";
      if(aside)aside.hidden=!state.inspector;
      if(splitter){
        splitter.hidden=!state.inspector;
        splitter.setAttribute("aria-valuemin",String(WIDTH.min));
        splitter.setAttribute("aria-valuemax",String(maxWidth()));
        splitter.setAttribute("aria-valuenow",String(width));
        splitter.setAttribute("aria-valuetext",width+" pixels");
      }
      if(widthInput){widthInput.min=String(WIDTH.min);widthInput.max=String(WIDTH.max);widthInput.step=String(WIDTH.step);widthInput.value=String(width);}
      if(widthOut)widthOut.textContent=state.width===null?"Default":width+" px";
      if(scaleInput)scaleInput.value=String(state.scale);
      for(const button of toggles){
        button.setAttribute("aria-pressed",String(!state.inspector));
        const label=state.inspector?"Hide build panel":"Show build panel";

        if(button.dataset.inspectorToggle==="text")button.textContent=label;
        else button.setAttribute("aria-label",label);
        button.title=label+" (\\)";
      }
    }
    function commit(next,persist=true){
      const previous=state;
      state=normalize({...state,...next});
      paint();
      if(persist)save();

      if(previous.width!==state.width || previous.inspector!==state.inspector || previous.scale!==state.scale)env.resize?.();
      return {...state};
    }
    const setWidth=(width,persist=true)=>commit({width:clampWidth(width)},persist);
    const setScale=scale=>commit({scale:Number(scale)});
    const setInspector=shown=>commit({inspector:!!shown});
    const toggleInspector=()=>setInspector(!state.inspector);
    const showInspector=()=>state.inspector?{...state}:setInspector(true);
    const reset=()=>commit({...DEFAULTS});

    if(splitter){
      splitter.onpointerdown=event=>{
        if(event.button!==undefined && event.button!==0)return;
        event.preventDefault();
        drag={x:event.clientX,width:current(),id:event.pointerId};
        splitter.setPointerCapture?.(event.pointerId);
        body.dataset.resizing="true";
      };
      splitter.onpointermove=event=>{
        if(!drag)return;

        setWidth(Math.min(maxWidth(),drag.width+(drag.x-event.clientX)),false);
      };
      const end=()=>{
        if(!drag)return;
        splitter.releasePointerCapture?.(drag.id);
        drag=null;delete body.dataset.resizing;save();
      };
      splitter.onpointerup=end;splitter.onpointercancel=end;splitter.onlostpointercapture=end;
      splitter.ondblclick=()=>commit({width:null});
      splitter.onkeydown=event=>{
        const big=event.shiftKey?WIDTH.step*4:WIDTH.step, width=current();
        const next={ArrowLeft:width+big,ArrowRight:width-big,Home:WIDTH.min,End:maxWidth()}[event.key];
        if(next!==undefined){event.preventDefault();setWidth(Math.min(maxWidth(),next));}
        else if(event.key==="Enter"){event.preventDefault();commit({width:null});}
      };
    }
    if(widthInput)widthInput.oninput=()=>setWidth(Number(widthInput.value));
    if(scaleInput)scaleInput.onchange=()=>setScale(scaleInput.value);
    for(const button of toggles)button.onclick=toggleInspector;


    for(const menu of ["layoutMenu","layersMenu"].map(el).filter(Boolean)){
      menu.addEventListener?.("keydown",event=>{
        if(event.key!=="Escape" || !menu.open)return;
        event.preventDefault();event.stopPropagation();
        menu.open=false;menu.querySelector("summary")?.focus();
      });
    }
    const resetButton=el("layoutReset");
    if(resetButton)resetButton.onclick=reset;
    paint();
    return {state:()=>({...state}),setWidth,setScale,setInspector,toggleInspector,showInspector,reset,refresh:paint};
  }
  const api={create,normalize,clampWidth,KEY,WIDTH,SCALES,DEFAULTS};
  if(typeof module==="object" && module.exports)module.exports=api;
  root.WaamWindow=api;
})(typeof window==="undefined"?globalThis:window);
