


(function(root){
  "use strict";
  const LABEL_STEPS=[1,2,5,10,20,25,50,100,200,250,500,1000];

  const sub=(p,q)=>[p[0]-q[0],p[1]-q[1],p[2]-q[2]];
  const dot=(p,q)=>p[0]*q[0]+p[1]*q[1]+p[2]*q[2];
  const scaled=(v,k)=>[v[0]*k,v[1]*k,v[2]*k];
  function unit(v){const n=Math.hypot(v[0],v[1],v[2]);return n>1e-9?scaled(v,1/n):null;}


  function frame(state){
    const substrate=state?.meta?.substrate;
    if(substrate && Array.isArray(substrate.points_robot_mm) && substrate.points_robot_mm.length===4){
      const o=substrate.origin_robot_mm||[0,0,0];
      const [c1,c2,,c4]=substrate.points_robot_mm.map(p=>sub(p,o));
      const a=unit(sub(c2,c1));
      if(!a)return null;

      const toward=sub(c4,c1), b=unit(sub(toward,scaled(a,dot(toward,a))));
      if(!b)return null;
      const lenA=dot(sub(c2,c1),a), lenB=dot(toward,b);
      if(!(lenA>0 && lenB>0))return null;
      return {kind:"substrate",origin:c1,a,b,lenA,lenB,labelA:"along edge 1→2 from corner 1",labelB:"square off edge 1→2 from corner 1",
        shortA:"from corner 1",shortB:"off edge 1→2"};
    }
    const box=(state?.scene?.boxes||[]).find(entry=>entry.name==="charles_platter");
    if(!box)return null;


    const z=box.max[2], origin=[box.min[0],box.min[1],z];
    const lenA=box.max[1]-box.min[1], lenB=box.max[0]-box.min[0];
    if(!(lenA>0 && lenB>0))return null;
    return {kind:"platter",origin,a:[0,1,0],b:[1,0,0],lenA,lenB,labelA:"along platter from faceplate end",labelB:"across platter from robot-side edge",
      shortA:"from faceplate end",shortB:"from robot-side edge"};
  }


  function extents(points,f){
    let a0=Infinity,a1=-Infinity,b0=Infinity,b1=-Infinity;
    for(const p of points){
      const d=sub(p,f.origin), a=dot(d,f.a), b=dot(d,f.b);
      if(a<a0)a0=a;if(a>a1)a1=a;if(b<b0)b0=b;if(b>b1)b1=b;
    }
    return Number.isFinite(a0)?{a:[a0,a1],b:[b0,b1]}:null;
  }


  function ticks(pxPerMm,minLabelPx){
    if(!(pxPerMm>0))return null;
    const major=LABEL_STEPS.find(step=>step*pxPerMm>=minLabelPx);
    if(!major)return null;
    const fifth=major/5, half=major/2;
    const minor=fifth*pxPerMm>=minLabelPx/7?fifth:half*pxPerMm>=minLabelPx/7?half:major;
    return {major,minor};
  }

  const fmt=v=>(Math.abs(v)<.05?0:v).toFixed(1);
  function summary(f,e){
    if(!f || !e)return "";
    const where=f.kind==="platter"?"platter":"substrate";
    return `Weld path on the ${where}: ${fmt(e.a[0])}–${fmt(e.a[1])} mm ${f.labelA}; `+
      `${fmt(e.b[0])}–${fmt(e.b[1])} mm ${f.labelB}.`;
  }


  function draw(ctx,proj,f,e,opts={}){
    const dpr=opts.dpr||1, ink=opts.ink||"#e9f2fa", weld=opts.weld||"#ffc86a";
    const dark="rgba(12,22,30,.72)";
    const P=p=>proj({x:p[0],y:p[1],z:p[2]});

    const pt=(a,b)=>[0,1,2].map(k=>f.origin[k]+f.a[k]*a+f.b[k]*b);
    const line=(p,q,color,width)=>{
      const u=P(p),v=P(q);if(!u[2] || !v[2])return;
      ctx.beginPath();ctx.moveTo(u[0],u[1]);ctx.lineTo(v[0],v[1]);
      ctx.strokeStyle=color;ctx.lineWidth=width*dpr;ctx.stroke();
    };


    const queued=[];
    const pill=(x,y,text,color,bg,rank)=>queued.push({x,y,text,color,bg,rank});
    const label=(p,text,color,bg,rank)=>{const u=P(p);if(u[2])pill(u[0],u[1],text,color,bg,rank);};
    const TAPE=0, BAND=1, TICK=2, CAPTION=3;
    ctx.save();
    ctx.lineCap="round";ctx.font=`${11*dpr}px Segoe UI, system-ui, sans-serif`;
    const rulers=[
      {along:(t,mm)=>pt(t,-mm),len:f.lenA,ext:e?.a,short:f.shortA,zero:true},
      {along:(t,mm)=>pt(-mm,t),len:f.lenB,ext:e?.b,short:f.shortB,zero:false},
    ];
    for(const r of rulers){
      const s0=P(r.along(0,0)), s1=P(r.along(r.len,0));
      if(!s0[2] || !s1[2])continue;
      const screenLen=Math.hypot(s1[0]-s0[0],s1[1]-s0[1]);
      if(screenLen<40*dpr)continue;
      const spacing=ticks(screenLen/r.len,42*dpr);
      if(!spacing)continue;
      const tickMm=Math.max(4,10/(screenLen/r.len)*dpr);
      line(r.along(0,0),r.along(r.len,0),ink,1.4);
      const count=Math.floor(r.len/spacing.minor+1e-9);
      for(let i=0;i<=count;i++){
        const t=i*spacing.minor, major=Math.abs(t/spacing.major-Math.round(t/spacing.major))<1e-6;
        line(r.along(t,0),r.along(t,major?tickMm:tickMm*.5),ink,major?1.2:.8);

        if(major && (t>0 || r.zero))label(r.along(t,tickMm*2.3),String(Math.round(t)),ink,dark,TICK);
      }
      label(r.along(r.len/2,tickMm*4.4),"mm "+r.short,"#bfe6ff",dark,CAPTION);
      if(!r.ext)continue;
      const lo=Math.max(0,Math.min(r.len,r.ext[0])), hi=Math.max(0,Math.min(r.len,r.ext[1]));
      line(r.along(lo,-1),r.along(hi,-1),weld,4);
      const u=P(r.along(lo,-tickMm*2.2)), v=P(r.along(hi,-tickMm*2.2));
      if(!u[2] || !v[2])continue;
      const one=fmt(r.ext[0]), two=fmt(r.ext[1]);
      if(one===two)pill(u[0],u[1],one,"#1b1405",weld,BAND);
      else if(Math.hypot(v[0]-u[0],v[1]-u[1])<ctx.measureText(one+two).width+20*dpr)
        pill((u[0]+v[0])/2,(u[1]+v[1])/2,`${one}–${two}`,"#1b1405",weld,BAND);
      else{pill(u[0],u[1],one,"#1b1405",weld,BAND);pill(v[0],v[1],two,"#1b1405",weld,BAND);}
    }
    if(e){

      const a=e.a[0], b=e.b[0], corner=pt(a,b), c=P(corner);
      ctx.setLineDash([5*dpr,4*dpr]);
      line(pt(0,b),corner,weld,1.3);
      line(pt(a,0),corner,weld,1.3);
      ctx.setLineDash([]);
      if(c[2]){
        ctx.beginPath();ctx.arc(c[0],c[1],3.5*dpr,0,2*Math.PI);ctx.fillStyle=weld;ctx.fill();
        for(const [from,value,short] of [[pt(0,b),a,f.shortA],[pt(a,0),b,f.shortB]]){
          const s=P(from);if(!s[2])continue;
          let dx=s[0]-c[0], dy=s[1]-c[1];
          const n=Math.hypot(dx,dy);
          if(n<1e-6){dx=0;dy=-1;}else{dx/=n;dy/=n;}
          const back=Math.min(52*dpr,Math.max(18*dpr,n/2));
          pill(c[0]+dx*back,c[1]+dy*back,`${fmt(value)} ${short}`,"#1b1405",weld,TAPE);
        }
      }
    }
    const placed=[], h=15*dpr, gap=2*dpr;
    ctx.textAlign="center";ctx.textBaseline="middle";
    for(const q of queued.sort((m,n)=>m.rank-n.rank)){
      const w=ctx.measureText(q.text).width+8*dpr;
      const box=[q.x-w/2-gap,q.y-h/2-gap,q.x+w/2+gap,q.y+h/2+gap];
      if(placed.some(o=>box[0]<o[2] && o[0]<box[2] && box[1]<o[3] && o[1]<box[3]))continue;
      placed.push(box);
      ctx.fillStyle=q.bg;ctx.fillRect(q.x-w/2,q.y-h/2,w,h);
      ctx.fillStyle=q.color;ctx.fillText(q.text,q.x,q.y+.5*dpr);
    }
    ctx.restore();
  }

  const api={frame,extents,ticks,summary,draw};
  if(typeof module==="object" && module.exports)module.exports=api;
  root.WaamScale=api;
})(typeof window==="undefined"?globalThis:window);
