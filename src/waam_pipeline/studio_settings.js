

(function(root){
  "use strict";
  const DEFAULTS=Object.freeze({theme:"graphite",density:"comfortable",reduce_motion:false,
    default_frame:"cell",default_camera:"iso",path_only:false});
  const CHOICES={theme:["graphite","drafting","operator"],density:["comfortable","compact"],
    default_frame:["cell","part"],default_camera:["iso","top"]};
  const KEYS=Object.keys(DEFAULTS);
  function normalize(value){
    const source=value && typeof value==="object" && !Array.isArray(value)?value:{};
    const result={...DEFAULTS};
    for(const key of KEYS){
      if(CHOICES[key]?CHOICES[key].includes(source[key]):typeof source[key]==="boolean") result[key]=source[key];
    }
    return result;
  }
  function create(env){
    const doc=env.document, el=id=>doc.getElementById(id);
    const dialog=el("settingsDlg"), trigger=el("settingsBtn");
    const tabs=Array.from(doc.querySelectorAll("[data-settings-tab]"));
    const themes=Array.from(doc.querySelectorAll("[data-settings-theme]"));
    const inputs=Array.from(doc.querySelectorAll("[data-preference-control]"));
    const request=env.fetch || ((...args)=>root.fetch(...args));
    let applied={...DEFAULTS}, draft={...applied}, category="appearance";
    let identity=null, mode="pending", ready=false, loading=false, saving=false;
    let ownerId=null, generation=0, previousFocus=null, localKey=null;
    let desktopLoad=0;
    let status="Checking your session…", error=false, localPersistent=true;

    function dirty(){return KEYS.some(key=>draft[key]!==applied[key]);}
    const blocked=()=>env.blocked?.()||"";
    function storage(){return env.storage!==undefined?env.storage:root.localStorage;}
    function appearance(){
      doc.body.dataset.look=applied.theme;
      doc.body.dataset.density=applied.density;
      doc.body.dataset.reduceMotion=String(applied.reduce_motion);
    }
    function scope(){
      if(mode==="account")return "Personal to your account.";
      if(mode==="offline")return "Saved in this browser for offline previews.";
      if(mode==="guest")return "Saved in this browser for guest previews.";
      if(mode==="shared")return "Saved in this browser, separate from this shared build.";
      if(mode==="unavailable")return "Session unavailable. Preferences have not been loaded.";
      return "Checking your session…";
    }
    function idleStatus(){
      if(loading)return "Loading your preferences…";
      if(!ready)return status;
      return dirty()?"Unapplied changes. Back to build leaves without applying.":scope();
    }
    function paint(message){
      if(message!==undefined)status=message;
      const writeBlocked=blocked();
      for(const button of themes)button.setAttribute("aria-pressed",String(button.dataset.settingsTheme===draft.theme));
      el("settingsDensity").value=draft.density;
      el("settingsDensityPreview").dataset.density=draft.density;
      el("settingsReduceMotion").checked=draft.reduce_motion;
      el("settingsDefaultFrame").value=draft.default_frame;
      el("settingsDefaultCamera").value=draft.default_camera;
      el("settingsPathOnly").checked=draft.path_only;
      for(const input of inputs)input.disabled=!ready || loading || saving;
      el("settingsApply").disabled=!ready || loading || saving || !dirty() || !!writeBlocked;
      el("settingsCancel").disabled=saving || !dirty();
      el("settingsBack").disabled=saving;
      el("settingsApply").textContent=saving?"Applying…":"Apply preferences";
      el("settingsStatus").textContent=writeBlocked||status;
      el("settingsStatus").dataset.error=String(error);
      el("settingsScope").textContent=scope();
      el("settingsRetry").hidden=!error || loading || saving || !identity || !!identity.unavailable;
      dialog.setAttribute("aria-busy",String(loading || saving));
    }
    function change(key,value){
      if(!ready || loading || saving)return;
      draft[key]=value;error=false;paint(idleStatus());
    }
    function commit(value,initial){
      const previous={...applied};
      applied=normalize(value);draft={...applied};appearance();


      env.applyPreview?.({...applied},previous,{initial:!!initial});
      if(previous.theme!==applied.theme || previous.density!==applied.density)env.resize?.();
    }
    const desktopCards=Array.from(doc.querySelectorAll("[data-desktop-platform]"));
    const desktopField=(card,name)=>card.querySelector('[data-desktop-field="'+name+'"]');
    function desktopMessage(message,{retry=false,errorState=false}={}){
      el("settingsDesktopState").hidden=false;
      el("settingsDesktopState").textContent=message;
      el("settingsDesktopState").dataset.error=String(errorState);
      el("settingsDesktopRetry").hidden=!retry;
      el("settingsDesktopRelease").hidden=true;
      for(const card of desktopCards){
        card.hidden=true;desktopField(card,"download").removeAttribute("href");
        card.querySelector("[data-desktop-copy]").disabled=true;
      }
    }
    function validDesktop(value){
      return !!value && typeof value.id==="string" && typeof value.url==="string" &&
        value.url==="/download/desktop/"+value.id &&
        typeof value.version==="string" && !!value.version &&
        Number.isSafeInteger(value.size) && value.size>0 &&
        typeof value.sha256==="string" && /^[0-9a-f]{64}$/.test(value.sha256) &&
        typeof value.platform==="string" && typeof value.signed==="string" &&
        typeof value.notarized==="boolean" && typeof value.published_at==="string" &&
        typeof value.notes==="string" &&
        (value.signature===undefined ||
          (typeof value.signature==="string" && /^[A-Za-z0-9+/]{86}==$/.test(value.signature)));
    }
    function desktopCard(card,value){
      desktopField(card,"version").textContent=value.version;
      desktopField(card,"platform").textContent=value.platform;
      desktopField(card,"size").textContent=(value.size/(1024*1024)).toFixed(1)+" MB";
      const date=new Date(value.published_at),published=desktopField(card,"published");
      published.textContent=Number.isNaN(date.getTime())?value.published_at:date.toLocaleString();
      published.setAttribute("datetime",value.published_at);

      desktopField(card,"trust").textContent=value.signed+
        (value.id==="macos-arm64"?"; "+(value.notarized?"notarized":"not notarized"):"");
      desktopField(card,"signature").textContent=value.signature===undefined?
        "not signed — install manually":"present";
      desktopField(card,"sha").textContent=value.sha256;
      const notes=desktopField(card,"notes");
      notes.textContent=value.notes;notes.hidden=!value.notes.trim();
      const download=desktopField(card,"download");
      download.href=value.url;download.setAttribute("download","");
      const copy=card.querySelector("[data-desktop-copy]");
      copy.textContent="Copy";copy.disabled=false;
      card.hidden=false;
    }

    function desktopRelease(value){
      if(!Array.isArray(value.releases) || !value.releases.every(validDesktop)){
        throw new Error("The server returned invalid desktop build information.");
      }
      let shown=0;
      for(const card of desktopCards){
        const release=value.releases.find(item=>item.id===card.dataset.desktopPlatform);
        card.hidden=true;
        if(release){desktopCard(card,release);shown++;}
      }
      if(!shown)return false;
      el("settingsDesktopOrigin").textContent=env.origin||(root.location&&root.location.origin)||"";
      el("settingsDesktopState").hidden=true;el("settingsDesktopRetry").hidden=true;
      el("settingsDesktopRelease").hidden=false;
      return true;
    }
    async function loadDesktop(){
      const current=identity, requestId=++desktopLoad, identityVersion=generation;
      if(!current){desktopMessage("Checking your session…");return false;}
      if(current.guest){desktopMessage("Sign in to download the desktop app.");return false;}
      if(current.offline){desktopMessage("Open the hosted workbench and sign in to download the desktop app.");return false;}
      if(current.unavailable || !current.ok){desktopMessage("Could not verify your session. Reload before downloading the desktop app.",{errorState:true});return false;}
      desktopMessage("Checking for a desktop build…");
      try{
        const response=await request(env.api("/api/desktop-release"),{credentials:"same-origin"});
        const result=await response.json();
        if(requestId!==desktopLoad || identityVersion!==generation)return false;
        if(response.status===403){desktopMessage("Sign in to download the desktop app.");return false;}
        if(!response.ok)throw new Error("Desktop build information is unavailable. Try again.");
        if(!result.available || !desktopRelease(result)){
          desktopMessage("No desktop build has been published on this server yet.");return true;
        }
        return true;
      }catch(failure){
        if(requestId!==desktopLoad || identityVersion!==generation)return false;
        desktopMessage(failure.message||"Desktop build information is unavailable. Try again.",
          {retry:true,errorState:true});return false;
      }
    }
    async function copyDesktopSha(card){
      try{
        const clipboard=env.clipboard||(root.navigator&&root.navigator.clipboard);
        if(!clipboard || typeof clipboard.writeText!=="function")throw new Error("Clipboard unavailable");
        await clipboard.writeText(desktopField(card,"sha").textContent);
        card.querySelector("[data-desktop-copy]").textContent="Copied";
        el("settingsDesktopState").hidden=false;el("settingsDesktopState").dataset.error="false";
        el("settingsDesktopState").textContent="SHA-256 copied.";
      }catch(_){
        el("settingsDesktopState").hidden=false;el("settingsDesktopState").dataset.error="true";
        el("settingsDesktopState").textContent="Could not copy automatically. Select the SHA-256 value and copy it.";
      }
    }
    function chooseTab(value,focus=false){
      if(!["appearance","preview","account","desktop"].includes(value))return;
      category=value;
      for(const tab of tabs){
        const selected=tab.dataset.settingsTab===category;
        tab.setAttribute("aria-selected",String(selected));tab.tabIndex=selected?0:-1;
        el("settings-"+tab.dataset.settingsTab).hidden=!selected;
        if(selected && focus)tab.focus();
      }
      if(category==="desktop")void loadDesktop();
    }
    function cancel(){
      if(saving)return false;
      draft={...applied};error=false;paint(ready?"Changes cancelled.":idleStatus());return true;
    }
    function open(){
      if(dialog.open)return;
      previousFocus=doc.activeElement;
      env.pause?.();
      draft={...applied};chooseTab(category);paint(idleStatus());
      dialog.showModal();trigger.setAttribute("aria-expanded","true");el("settingsBack").focus();
    }
    function close(){
      if(saving)return false;
      draft={...applied};paint(idleStatus());
      if(dialog.open)dialog.close();
      return true;
    }
    function renderIdentity(j){
      let title="Session unavailable", note="Reload to verify your session and load personal preferences.";
      if(j.offline){title="Offline preview";note="Appearance and preview preferences are stored in this browser. Open the hosted workbench to manage your account.";}
      else if(j.ok && j.sso){title=j.email||"Signed in";note="Personal preferences follow your account. Sign-in methods are managed below.";}
      else if(j.ok && j.guest){title="Guest";note="Preferences stay in this browser. Sign in to use saved builds and account preferences.";}
      else if(j.ok){title="Shared workspace";note="These browser preferences are personal to this device and do not change the shared build.";}
      el("settingsIdentity").textContent=title;
      el("settingsAccountNote").textContent=note;
      el("settingsAuthActions").hidden=!!j.offline || !!j.unavailable;
      el("settingsPasskeys").hidden=!(j.ok && j.sso);
    }
    async function setIdentity(j){
      identity=j||{unavailable:true};
      const version=++generation;
      desktopLoad++;
      ready=false;loading=false;saving=false;ownerId=null;localKey=null;error=false;
      applied={...DEFAULTS};draft={...applied};appearance();renderIdentity(identity);
      if(category==="desktop")void loadDesktop();
      if(identity.unavailable || (!identity.ok && !identity.offline)){
        mode="unavailable";status="Could not verify your session. Reload before saving preferences.";paint();return false;
      }
      if(identity.ok && identity.sso){
        mode="account";loading=true;paint("Loading your account preferences…");
        const expectedOwner=identity.library_owner;
        if(typeof expectedOwner!=="string" || !expectedOwner){
          loading=false;error=true;paint("Could not verify the preference owner. Reload to try again.");return false;
        }
        try{
          const response=await request(env.api("/api/preferences",{owner:expectedOwner}),{credentials:"same-origin"});
          const result=await response.json();
          if(version!==generation)return false;
          if(!response.ok || !result.ok || result.owner_id!==expectedOwner){
            throw new Error(response.status===409 || (result.owner_id && result.owner_id!==expectedOwner)?
              "Your sign-in changed. Reload before saving preferences.":"Could not load your preferences. Try again.");
          }
          ownerId=result.owner_id;ready=true;loading=false;commit(result.preferences,true);
          paint(scope());return true;
        }catch(failure){
          if(version!==generation)return false;
          loading=false;error=true;paint(failure.message||"Could not load your preferences. Try again.");return false;
        }
      }


      mode=identity.offline?"offline":identity.guest?"guest":"shared";
      localKey="waamStudioPreferences:"+(mode==="offline"?"offline":"guest")+":v1";
      let value=DEFAULTS;localPersistent=true;
      try{value=JSON.parse(storage()?.getItem(localKey)||"null")||DEFAULTS;}
      catch(_){localPersistent=false;}
      ready=true;commit(value,true);paint(localPersistent?scope():"Preferences apply for this visit; browser storage is unavailable.");return true;
    }
    async function apply(){
      if(!ready || loading || saving || !dirty())return false;
      if(blocked()){error=false;paint();return false;}
      if(env.busy?.()){error=false;paint("Wait for the current build update to finish, then apply preferences.");return false;}
      const next=normalize(draft), version=generation, expectedOwner=ownerId;
      saving=true;error=false;paint("Applying preferences…");
      if(mode==="account"){
        try{
          const response=await request(env.api("/api/preferences",{owner:expectedOwner}),{
            method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify(next)});
          const result=await response.json();
          if(version!==generation || expectedOwner!==ownerId)return false;
          if(!response.ok || !result.ok || result.owner_id!==expectedOwner){
            throw new Error(response.status===409 || (result.owner_id && result.owner_id!==expectedOwner)?
              "Your sign-in changed. Reload before saving preferences.":"Preferences were not saved. Try Apply again.");
          }
          saving=false;commit(result.preferences,false);paint("Preferences saved to your account.");return true;
        }catch(failure){
          if(version!==generation)return false;
          saving=false;error=true;paint(failure.message||"Preferences were not saved. Try Apply again.");return false;
        }
      }
      try{const store=storage();if(!store)throw new Error("No browser storage");store.setItem(localKey,JSON.stringify(next));localPersistent=true;}
      catch(_){localPersistent=false;}
      saving=false;commit(next,false);
      paint(localPersistent?"Preferences saved in this browser.":"Applied for this visit; browser storage is unavailable.");return true;
    }


    for(const id of ["signIn","signOut"]){const node=el(id);if(node)el("settingsAuthActions").appendChild(node);}
    const passkeys=el("passkeySection");if(passkeys)el("settingsPasskeys").appendChild(passkeys);
    trigger.onclick=open;el("settingsBack").onclick=close;
    el("settingsApply").onclick=apply;el("settingsCancel").onclick=cancel;
    el("settingsRetry").onclick=()=>ready && dirty()?apply():setIdentity(identity);
    el("settingsDesktopRetry").onclick=loadDesktop;
    for(const card of desktopCards)card.querySelector("[data-desktop-copy]").onclick=()=>copyDesktopSha(card);
    dialog.addEventListener("cancel",event=>{event.preventDefault();close();});
    dialog.addEventListener("close",()=>{
      draft={...applied};trigger.setAttribute("aria-expanded","false");
      (previousFocus?.isConnected?previousFocus:trigger).focus();
    });


    dialog.addEventListener("keydown",event=>event.stopPropagation());
    for(const tab of tabs){
      tab.onclick=()=>chooseTab(tab.dataset.settingsTab);
      tab.onkeydown=event=>{
        if(!["ArrowUp","ArrowDown","ArrowLeft","ArrowRight","Home","End"].includes(event.key))return;
        event.preventDefault();
        const at=tabs.indexOf(tab), length=tabs.length;
        const next=event.key==="Home"?0:event.key==="End"?length-1:
          (at+(["ArrowRight","ArrowDown"].includes(event.key)?1:-1)+length)%length;
        chooseTab(tabs[next].dataset.settingsTab,true);
      };
    }
    for(const theme of themes)theme.onclick=()=>change("theme",theme.dataset.settingsTheme);
    for(const [id,key,checkbox] of [["settingsDensity","density",false],["settingsReduceMotion","reduce_motion",true],
      ["settingsDefaultFrame","default_frame",false],["settingsDefaultCamera","default_camera",false],["settingsPathOnly","path_only",true]]){
      el(id).onchange=()=>change(key,checkbox?el(id).checked:el(id).value);
    }
    appearance();chooseTab(category);paint();
    return {open,close,apply,cancel,chooseTab,setIdentity,loadDesktop,paint,isOpen:()=>dialog.open,
      reducedMotion:()=>applied.reduce_motion,preferences:()=>({...applied})};
  }
  const api={create,normalize,defaults:DEFAULTS};
  if(typeof module==="object" && module.exports)module.exports=api;
  root.WaamSettings=api;
})(typeof window==="undefined"?globalThis:window);
