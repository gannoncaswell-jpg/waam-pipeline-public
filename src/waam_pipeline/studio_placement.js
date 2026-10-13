
(function(root){
  "use strict";
  const TAU=2*Math.PI;
  const wrap=a=>((a+180)%360+360)%360-180;
  const snap=(v,step)=>step>0?Math.round(v/step)*step:v;
  const orientationFields=["rotX","rotY","lay_flat","tilt"];
  const equalFields=(a,b,keys)=>keys.every(k=>Math.abs(Number(a[k]||0)-Number(b[k]||0))<1e-7);
  const same=(a,b)=>equalFields(a,b,["ox","oy","yaw",...orientationFields]);
  function transform(p,t){
    if(!t) return p;
    const a=t.angle*Math.PI/180,c=Math.cos(a),s=Math.sin(a);
    if(t.plane_basis){
      const [u,v,n]=t.plane_basis,delta=[p.x-t.pivot[0],p.y-t.pivot[1],p.z-t.pivot[2]];
      const dot=b=>b.reduce((sum,value,i)=>sum+value*delta[i],0);
      const x=dot(u),y=dot(v),z=dot(n);
      const q=u.map((value,i)=>t.pivot[i]+value*(x*c-y*s+t.dx)+v[i]*(x*s+y*c+t.dy)+n[i]*z);
      return {x:q[0],y:q[1],z:q[2]};
    }
    const x=p.x-t.pivot[0],y=p.y-t.pivot[1];
    return {x:t.pivot[0]+x*c-y*s+t.dx,y:t.pivot[1]+x*s+y*c+t.dy,z:p.z};
  }
  function planeDelta(dx,dy,x,y){
    const det=x[0]*y[1]-x[1]*y[0];


    if(Math.abs(det)<0.08*Math.max(1e-10,x[0]**2+x[1]**2,y[0]**2+y[1]**2)) return null;
    return [(dx*y[1]-dy*y[0])/det,(dy*x[0]-dx*x[1])/det];
  }
  function axisDelta(dx,dy,axis){
    const n=axis[0]*axis[0]+axis[1]*axis[1];
    return n>1e-10?(dx*axis[0]+dy*axis[1])/n:null;
  }
  function segmentDistance(p,a,b){
    const x=b[0]-a[0],y=b[1]-a[1],n=x*x+y*y;
    const t=n?Math.max(0,Math.min(1,((p[0]-a[0])*x+(p[1]-a[1])*y)/n)):0;
    return Math.hypot(p[0]-a[0]-t*x,p[1]-a[1]-t*y);
  }
  function cameraAfterPlacement(camera,before,after){
    return {...camera,center:camera.center.map((v,i)=>v+before[i]-after[i])};
  }
  function create(env){
    const doc=env.document, get=id=>doc.getElementById(id);
    let mode="move",gesture=null,undo=[],redo=[],action=null,applying=false;
    const fields={ox:"wbOx",oy:"wbOy",yaw:"wbYaw",rotX:"wbRotX",rotY:"wbRotY",lay_flat:"wbLayFlat",tilt:"wbTilt"};
    const blockReason=()=>env.blocked?.()||"";
    function writeReady(){
      const reason=blockReason();
      if(reason){get("placementStatus").textContent=reason;return false;}
      return !env.busy();
    }
    function actual(){
      const p=env.state().meta.params||{};
      return Object.fromEntries(Object.keys(fields).map(k=>[k,Number(p[k]||0)]));
    }
    function values(){
      const result={};
      for(const [k,id] of Object.entries(fields)){
        const el=get(id),v=Number(el.value);
        if(!el.value.trim()||!Number.isFinite(v)||v<Number(el.min)||v>Number(el.max)) return null;
        result[k]=v;
      }
      return result;
    }
    function write(value){
      for(const [key,id] of Object.entries(fields)){
        const el=get(id);el.value=String(value[key]||0);
        if(env.validate) env.validate(el);
      }
    }
    function enabled(){return env.state().meta.workbench && !!env.state().meta.placement && env.step()==="place";}
    function pending(){const v=values();return !!env.state().meta.placement&&(!v||!same(v,actual()));}
    function preview(){
      if(!enabled()) return null;
      const v=values(),base=actual(),p=env.state().meta.placement;
      if(!v||same(v,base)||!p.pivot||!equalFields(v,base,orientationFields)) return null;
      return {dx:v.ox-base.ox,dy:v.oy-base.oy,angle:v.yaw-base.yaw,
        pivot:p.display_pivot||p.pivot,...(p.plane_basis?{plane_basis:p.plane_basis}:{})};
    }
    function update(){
      const on=enabled(),v=values(),draft=pending();
      get("placementToolbar").hidden=!on;
      get("placementPreview").hidden=!draft;
      const reference=env.state().meta.cell_profile && env.state().meta.cell_profile.planning_only;
      get("placementPreview").textContent=draft?(v?(reference?"Placement preview · Apply to accept":"Placement preview · Apply & check to accept"):"Enter valid placement values or revert"):"";
      if(v&&!equalFields(v,actual(),orientationFields)) get("placementPreview").textContent="Orientation pending · Apply to snap to a face and reslice";
      const mesh=!!env.state().meta.stl,face=env.state().meta.stl?.orientation;
      const writeBusy=env.busy()||!!blockReason();
      for(const id of ["wbRotX","wbRotY","placeLayFlat","placeResetOrientation"]) get(id).disabled=writeBusy||!mesh;
      get("faceStatus").textContent=face?`Seated on a ${Number(face.area_mm2).toFixed(1)} mm² face · ${face.candidate_count} supporting faces`:
        mesh?"Angles choose the nearest supporting face. Changing a tilt snaps and reslices.":"Upload an STL to place it on a face.";
      for(const button of doc.querySelectorAll("[data-place-mode]")){
        button.setAttribute("aria-pressed",String(button.dataset.placeMode===mode));
        button.disabled=env.busy()||!on;
      }
      for(const button of doc.querySelectorAll("[data-place-nudge]"))button.disabled=writeBusy||!on;
      get("placeCenter").disabled=writeBusy||!on;
      get("placeUndo").disabled=writeBusy||!undo.length||!!draft;
      get("placeRedo").disabled=writeBusy||!redo.length||!!draft;
      get("placeRevert").disabled=writeBusy||!draft;
      get("play").disabled=env.busy()||!!draft;
      const badge=get("clearanceBadge");
      if(draft){badge.dataset.state="pending";badge.textContent="Placement preview · not checked";}
      else env.clearance();
      get("movePart").checked=on&&mode!=="orbit";
      if(on) get("hud").textContent="Drag a handle · Right-drag to orbit · Shift-drag to pan · Esc cancels";
    }
    function refresh(){env.changed?.();update();env.draw();}
    function cancel(restore=true,event=null){
      if(event&&gesture&&event.pointerId!==gesture.id) return;
      if(gesture && restore) write(gesture.initial);
      gesture=null;refresh();
    }
    function revert(){if(!writeReady())return;gesture=null;write(actual());refresh();}
    function setMode(next){
      if(env.busy()||!["move","rotate","orbit"].includes(next)) return;
      cancel();mode=next;env.pause();refresh();
    }
    function layout(){
      if(!enabled()) return null;
      const p=env.state().meta.placement;if(!p.pivot) return null;
      const t=preview(),axes=p.plane_basis||[[1,0,0],[0,1,0],[0,0,1]],pivot=p.display_pivot||[p.pivot[0],p.pivot[1],p.bottom_z];
      const xyz=pivot.map((v,i)=>v+axes[0][i]*(t?t.dx:0)+axes[1][i]*(t?t.dy:0));
      const center={x:xyz[0],y:xyz[1],z:xyz[2]};
      const at=env.project(center),dpr=env.dpr(),scale=at[2];
      if(!Number.isFinite(scale)||scale<=0) return null;
      const basis=axis=>{const q=env.project({x:center.x+axis[0],y:center.y+axis[1],z:center.z+axis[2]});return [q[0]-at[0],q[1]-at[1]];};
      const x=basis(axes[0]),y=basis(axes[1]),length=82*dpr/scale;
      const tips=[x,y].map(v=>[at[0]+v[0]*length,at[1]+v[1]*length]);
      const radius=72*dpr/scale,ring=[];
      for(let i=0;i<=64;i++){
        const a=i*TAU/64;
        const q=xyz.map((v,i)=>v+radius*(Math.cos(a)*axes[0][i]+Math.sin(a)*axes[1][i]));
        ring.push(env.project({x:q[0],y:q[1],z:q[2]}));
      }
      if(![...x,...y,...tips.flat(),...ring.flat()].every(Number.isFinite)) return null;
      return {at,x,y,tips,ring,dpr,center};
    }
    function draw(ctx){
      const l=layout();if(!l||mode==="orbit") return;
      ctx.save();ctx.lineWidth=3*l.dpr;ctx.lineJoin="round";
      const poly=points=>{ctx.beginPath();points.forEach((p,i)=>i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1]));ctx.stroke();};
      if(mode==="rotate"){
        ctx.strokeStyle="#155dbe";poly(l.ring);
        ctx.fillStyle="#114984";ctx.font=`bold ${12*l.dpr}px Segoe UI`;
        ctx.fillText("Z rotation",l.ring[8][0]+8*l.dpr,l.ring[8][1]);
      }else{
        l.tips.forEach((tip,i)=>{
          const color=i?"#13854b":"#ce3e43",angle=Math.atan2(tip[1]-l.at[1],tip[0]-l.at[0]);
          if(Math.hypot(tip[0]-l.at[0],tip[1]-l.at[1])<14*l.dpr) return;
          ctx.strokeStyle=color;poly([l.at,tip]);ctx.fillStyle=color;
          ctx.beginPath();ctx.moveTo(tip[0],tip[1]);
          for(const a of [angle-0.45,angle+0.45]) ctx.lineTo(tip[0]-14*l.dpr*Math.cos(a),tip[1]-14*l.dpr*Math.sin(a));
          ctx.closePath();ctx.fill();ctx.font=`bold ${13*l.dpr}px Segoe UI`;
          ctx.fillText(i?"Y":"X",tip[0]+9*l.dpr,tip[1]-6*l.dpr);
        });
      }
      ctx.fillStyle="#f4faff";ctx.strokeStyle="#174f75";ctx.lineWidth=2*l.dpr;
      ctx.beginPath();ctx.arc(l.at[0],l.at[1],9*l.dpr,0,TAU);ctx.fill();ctx.stroke();
      ctx.restore();
    }
    function point(e){
      const r=env.canvas.getBoundingClientRect();
      return [(e.clientX-r.left)*env.canvas.width/r.width,(e.clientY-r.top)*env.canvas.height/r.height];
    }
    function down(e){
      if(gesture) return true;
      if(!enabled()||!writeReady()||e.button!==0||e.shiftKey||mode==="orbit") return false;
      const l=layout(),initial=values();if(!l||!initial) return false;
      const q=point(e),near=Math.hypot(q[0]-l.at[0],q[1]-l.at[1])<14*l.dpr;
      let axis=null;
      if(mode==="move"){
        if(near) axis="xy";
        else for(let i=0;i<2;i++) if(segmentDistance(q,l.at,l.tips[i])<10*l.dpr && Math.hypot(l.tips[i][0]-l.at[0],l.tips[i][1]-l.at[1])>=14*l.dpr){axis=i?"y":"x";break;}
      }else if(l.ring.some((p,i)=>i&&segmentDistance(q,l.ring[i-1],p)<11*l.dpr)) axis="rotate";
      if(!axis) return false;
      if((axis==="xy"||axis==="rotate")&&!planeDelta(0,0,l.x,l.y)){
        get("placementStatus").textContent="Use Top view for a clear view of the placement plane.";return true;
      }
      const polar=planeDelta(q[0]-l.at[0],q[1]-l.at[1],l.x,l.y);
      gesture={id:e.pointerId,start:q,initial,layout:l,axis,moved:false,
        lastAngle:polar?Math.atan2(polar[1],polar[0])*180/Math.PI:0,angle:0};
      env.pause();env.canvas.setPointerCapture(e.pointerId);return true;
    }
    function move(e){
      if(!gesture||gesture.id!==e.pointerId) return false;
      if(!writeReady()){cancel();return true;}
      const g=gesture,l=g.layout,q=point(e),dx=q[0]-g.start[0],dy=q[1]-g.start[1];
      const next={...g.initial},step=e.altKey?0:Number(get("placeSnap").value),turn=e.altKey?0:Number(get("rotateSnap").value);
      if(g.axis==="rotate"){
        const p=planeDelta(q[0]-l.at[0],q[1]-l.at[1],l.x,l.y);if(!p) return true;
        const a=Math.atan2(p[1],p[0])*180/Math.PI;
        g.angle+=wrap(a-g.lastAngle);g.lastAngle=a;next.yaw=snap(g.initial.yaw+g.angle,turn);
      }else if(g.axis==="xy"){
        const d=planeDelta(dx,dy,l.x,l.y);if(!d) return true;
        next.ox=snap(g.initial.ox+d[0],step);next.oy=snap(g.initial.oy+d[1],step);
      }else{
        const key=g.axis==="x"?"ox":"oy",d=axisDelta(dx,dy,g.axis==="x"?l.x:l.y);
        if(d===null) return true;next[key]=snap(g.initial[key]+d,step);
      }
      for(const [k,id] of Object.entries(fields)) next[k]=Math.max(Number(get(id).min),Math.min(Number(get(id).max),next[k]));
      g.moved=g.moved||Math.hypot(dx,dy)>3*l.dpr;
      write(next);refresh();return true;
    }
    function up(e){
      if(!gesture||gesture.id!==e.pointerId) return false;
      const g=gesture;gesture=null;
      if(!writeReady()){write(g.initial);refresh();return true;}
      if(g.moved&&!same(values()||g.initial,g.initial)) env.apply();else{write(g.initial);refresh();}
      return true;
    }
    function begin(){
      applying=true;gesture=null;update();
      return {transform:actual(),origin:env.state().meta.placement.origin.slice(),camera:env.camera?env.camera():null};
    }
    function accepted(snapshot){
      const before=snapshot.transform;
      const after=actual();
      if(action==="undo"){if(!same(before,after)){undo.pop();redo.push(before);}}
      else if(action==="redo"){if(!same(before,after)){redo.pop();undo.push(before);}}
      else if(!same(before,after)){undo.push(before);if(undo.length>30) undo.shift();redo=[];}
      if(snapshot.camera&&env.restoreCamera) env.restoreCamera(cameraAfterPlacement(
        snapshot.camera,snapshot.origin,env.state().meta.placement.origin));
      action=null;applying=false;refresh();
    }
    function rejected(){
      if(action==="undo"||action==="redo")write(actual());
      action=null;applying=false;gesture=null;refresh();
    }
    function reset(){if(applying) return;gesture=null;undo=[];redo=[];action=null;update();}
    async function history(which){
      const v=values();if(!writeReady()||!v||!same(v,actual())) return;
      const stack=which==="undo"?undo:redo;if(!stack.length) return;
      action=which;write(stack[stack.length-1]);
      try{if(await env.apply()===false && action===which) rejected();}
      catch(error){rejected();get("placementStatus").textContent="Placement failed: "+error.message;}
    }
    function nudge(axis,direction){
      if(!writeReady())return;const v=values();if(!v) return;
      const key=axis==="x"?"ox":axis==="y"?"oy":"yaw";
      v[key]+=direction*Number(get(key==="yaw"?"rotateStep":"placeStep").value);
      write(v);refresh();env.apply();
    }
    for(const b of doc.querySelectorAll("[data-place-mode]")) b.onclick=()=>setMode(b.dataset.placeMode);
    for(const b of doc.querySelectorAll("[data-place-nudge]")) b.onclick=()=>{const [axis,d]=b.dataset.placeNudge.split(",");nudge(axis,Number(d));};
    get("placeUndo").onclick=()=>history("undo");get("placeRedo").onclick=()=>history("redo");get("placeRevert").onclick=revert;
    get("placeCenter").onclick=()=>{
      if(!writeReady())return;const p=env.state().meta.placement,v=values();if(!p||!p.center_offsets||!v) return;

      if(!equalFields(v,actual(),["yaw",...orientationFields])){get("placementStatus").textContent="Apply the rotation before centering the part.";return;}
      write({...v,ox:p.center_offsets[0],oy:p.center_offsets[1]});refresh();env.apply();
    };
    for(const id of Object.values(fields)) get(id).addEventListener("input",()=>{gesture=null;env.pause();refresh();});
    for(const id of ["wbRotX","wbRotY"]) get(id).addEventListener("change",()=>{
      if(!writeReady()||!env.state().meta.stl||!values()) return;
      get("wbOz").value="0";refresh();env.apply();
    });
    get("placeLayFlat").onclick=()=>{
      if(!writeReady()||!env.state().meta.stl) return;const v=values();if(!v) return;
      write({...v,lay_flat:1});get("wbOz").value="0";refresh();return env.apply();
    };
    get("placeResetOrientation").onclick=()=>{
      if(!writeReady()||!env.state().meta.stl) return;const v=values();if(!v) return;
      write({...v,rotX:0,rotY:0,lay_flat:0,tilt:0});refresh();return env.apply();
    };
    get("movePart").onchange=()=>setMode(get("movePart").checked?"move":"orbit");
    update();
    return {draw,preview,pending,down,move,up,cancel,revert,update,begin,accepted,rejected,reset,
      hasGesture:()=>!!gesture,isApplying:()=>applying,
      setMode,history,values,actual};
  }
  root.WaamPlacement={create,transform,planeDelta,axisDelta,segmentDistance,cameraAfterPlacement,wrap,snap};
})(typeof module==="object"&&module.exports?module.exports:window);
