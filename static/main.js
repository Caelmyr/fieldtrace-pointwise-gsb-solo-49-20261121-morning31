import {
  api,badge,button,check,choices,data,definition,detail,empty,field,file,frameCards,h,human,img,jsonField,
  link,list,lookups,metric,notice,num,pane,pct,pick,q,recordLink,route,row,send,setLookups,showModal,stamp,table,title,workspace,
} from "./ui.js";
import {frameWorkbench} from "./workspace.js";

const navItems=[["home","Overview"],["frames","Photos"],["events","Events"],["stations","Stations"],
  ["identities","Individuals"],["surveys","Survey reports"],["evidence","Evidence"]];
const selected=new Set();
const filter={deployment_id:"",review:"",illumination:"",offset:0,limit:36,sort:"newest"};
let sidebar;
let routeRun=0;

const depChoices=(catalog)=>catalog.deployments.map(x=>[x.id,x.name]);
const stationChoices=(catalog)=>catalog.stations.map(x=>[x.id,x.name]);
const catalog=()=>api("/api/stations");
const frameOpen=(key)=>route("frame",key);
const refresh=()=>render();
const getFrameList=async()=> (await api("/api/frames?limit=200&sort=newest")).frames;
function routeState(){const [section,key]=location.hash.slice(1).split("/");return [section||"home",key||null];}
function navigation(active){
  const map={frame:"frames",event:"events",person:"identities",report:"surveys",sample:"surveys",packet:"evidence"};
  q("#nav").replaceChildren(...navItems.map(([tab,name])=>h("a",{href:`#${tab}`,"aria-current":(map[active]||active)===tab?"page":null},name)));
}
async function bootDemo(){
  const result=await api("/api/demo",{method:"POST"});
  notice(result.reused?"Opened the synthetic training survey.":"Synthetic training survey added.");
  filter.deployment_id=result.deployments[0];
  route("frames");
}
async function home(){
  const info=await api("/api/home"), c=info.counts;
  return [
    h("section",{class:"hero"},h("div",null,h("p",{class:"kicker"},"Field camera workspace"),
      h("h1",null,"Read the trail."),
      h("p",null,"Review camera photos, keep the evidence behind each sighting, and compare activity with recorded survey effort."),
      h("div",{class:"actions"},button(info.training?"Open training survey":"Load training survey",bootDemo,"primary"),
        link("Browse photos","#frames","download"))),
      img("/static/training-look.jpg","Synthetic woodland scene with an animal crossing the trail","")),
    h("section",{class:"stats"},metric("Camera stations",c.stations),metric("Camera deployments",c.deployments),metric("Photos",c.frames),
      metric("Trigger events",c.events),metric("Awaiting review",c.pending)),
    h("div",{class:"topline"},h("h2",null,"Recent photos"),link("Open photo intake","#frames")),
    info.recent.length?frameCards(info.recent,null,frameOpen):empty("Load the synthetic survey or add a station to begin."),
  ];
}
function startEnd(now=new Date()){
  const one=new Date(now);one.setUTCDate(one.getUTCDate()-2);
  const two=new Date(now);two.setUTCDate(two.getUTCDate()+6);
  return [one.toISOString().slice(0,19),two.toISOString().slice(0,19)];
}
function iso(text){if(!text)return "";const value=new Date(text.endsWith("Z")||/[+-]\d\d:\d\d$/.test(text)?text:text+"Z");if(isNaN(value))throw new Error("Use a valid date and time.");return value.toISOString();}
function stationForm(existing=null){
  const curr=existing||{};
  showModal(existing?"Edit station":"Add camera station",[
    field("Station name","name",curr.name||"",{required:true}),field("Station code","code",curr.code||""),
    pick("Habitat","habitat",lookups.habitats,curr.habitat||"woodland"),
    field("IANA time zone","timezone",curr.timezone||"UTC",{required:true}),
    check("Withhold this station name in shared registers","private",curr.private),
    field("Survey site notes","note",curr.note||"",{type:"area",wide:true}),
  ],async (form,values)=>{
    values.private=form.elements.private.checked;
    if(existing)values.version=existing.version;
    await send(existing?`/api/stations/${existing.id}`:"/api/stations",values,existing?"PUT":"POST");
    notice("Station saved.");await refresh();
  });
}
function deployForm(options,existing=null,seed=""){
  const curr=existing||{};
  const dates=startEnd();
  showModal(existing?"Edit deployment":"Add camera deployment",[
    pick("Station","station_id",stationChoices(options),curr.station_id||seed),
    field("Deployment title","name",curr.name||"",{required:true}),field("Camera identifier","camera",curr.camera||""),
    field("IANA time zone","timezone",curr.timezone||"",{help:"Leave blank to use the station time zone."}),
    field("Start time (UTC)","started_at",curr.started_at?.slice(0,19)||dates[0],{type:"datetime-local",required:true}),
    field("End time (UTC)","ended_at",curr.ended_at?.slice(0,19)||dates[1],{type:"datetime-local",required:true}),
    field("Camera clock fast by seconds","clock_fast_s",curr.clock_fast_s||0,{type:"number"}),
    field("Clock uncertainty in seconds","clock_uncertainty_s",curr.clock_uncertainty_s||0,{type:"number",min:0}),
    pick("State","state",lookups.deployments,curr.state||"active"),field("Notes","note",curr.note||"",{wide:true,type:"area"}),
  ],async (_,values)=>{
    values.started_at=iso(values.started_at);values.ended_at=iso(values.ended_at);
    if(existing)values.version=existing.version;
    await send(existing?`/api/deployments/${existing.id}`:"/api/deployments",values,existing?"PUT":"POST");
    notice("Deployment saved.");await refresh();
  });
}
function outageForm(deployment,old=null){
  const current=old||{};
  const dates=startEnd();
  showModal(old?"Edit camera outage":"Record camera outage",[
    field("Start (UTC)","start",current.start_utc?.slice(0,19)||deployment.started_at?.slice(0,19)||dates[0],{type:"datetime-local"}),
    field("End (UTC)","end",current.end_utc?.slice(0,19)||deployment.ended_at?.slice(0,19)||dates[1],{type:"datetime-local"}),
    pick("Reason","cause",lookups.outages,current.cause||"service"),field("Note","note",current.note||"",{type:"area",wide:true}),
  ],async (_,values)=>{
    values.start=iso(values.start);values.end=iso(values.end);values.deployment_id=deployment.id;
    if(old)values.version=old.version;
    await send(old?`/api/outages/${old.id}`:"/api/outages",values,old?"PUT":"POST");notice("Outage saved.");await refresh();
  });
}
async function deploymentOutages(dep){
  const info=await api(`/api/deployments/${dep.id}/outages`);
  showModal("Recorded camera outages",[
    button("Record camera outage",()=>outageForm(dep),"primary"),
    h("div",{class:"full"},info.outages.length?info.outages.map(item=>row(item.name,`${item.start_utc} – ${item.end_utc}`,
      button("Edit",()=>outageForm(dep,item)))):empty("No recorded interruption.")),
  ],()=>true,{confirm:"Done"});
}
function cameraLog(cat,initial){
  const preview=h("div",{class:"full"});
  let result;
  const dialog=showModal("Reconcile camera log",[
    pick("Deployment","deployment_id",depChoices(cat),initial||""),
    field("Camera CSV log","csv","filename,captured_at\n",{type:"area",wide:true,required:true,help:"Use exact camera filenames and observed times. Preview before applying."}),
    button("Preview log",async()=>{const values=data(dialog.form);result=await send("/api/camera-log/preview",values);
      preview.replaceChildren(table(["Line","Photo","Previous UTC","Proposed UTC","Status","Use"],result.rows.map(r=>[
        r.line,r.filename,r.previous_utc,r.proposed_utc,`${r.status}${r.reason?": "+r.reason:""}`,
        r.status==="ready"?h("input",{type:"checkbox",name:"line",value:r.line,checked:true,"aria-label":`Use CSV line ${r.line}`}):"—"])));
    }),preview,
  ],async(form,values)=>{
    if(!result)throw new Error("Preview the camera log first.");
    values.selected=new FormData(form).getAll("line").map(Number);
    values.versions=Object.fromEntries(result.rows.filter(r=>r.frame_id).map(r=>[r.frame_id,r.version]));
    values.deployment_version=result.deployment_version;
    const response=await send("/api/camera-log/apply",values);
    notice(`${response.changed.length} camera times recorded.`);await refresh();
  },{confirm:"Apply selected times"});
}
async function stations(){
  const cat=await catalog();
  return [title("Camera stations","Organize deployments, camera clocks and recorded survey interruptions.",
    button("Add station",()=>stationForm(),"primary"),
    cat.stations.length&&button("Add deployment",()=>deployForm(cat))),
    h("div",{class:"grid cards"},cat.stations.map(site=>{
      const deploys=cat.deployments.filter(x=>x.station_id===site.id);
      return pane(site.name,h("p",{class:"muted"},`${human(lookups.habitats,site.habitat)} · ${site.timezone}`),
        site.note&&h("p",null,site.note),deploys.map(dep=>row(dep.name,`${dep.frame_count} photos · ${dep.outage_count} outages`,
          button("Photos",()=>{filter.deployment_id=dep.id;filter.offset=0;route("frames");}),
          button("Clock log",()=>cameraLog(cat,dep.id)),
          button("Outages",()=>deploymentOutages(dep)),button("Edit",()=>deployForm(cat,dep)))),
        h("div",{class:"actions"},button("Add deployment",()=>deployForm(cat,null,site.id)),
          button("Edit station",()=>stationForm(site))));
    })),!cat.stations.length&&empty("No camera stations. Add a station or load the training survey.")];
}
function intake(cat){
  if(!cat.deployments.length){notice("Add a deployment before importing.");route("stations");return;}
  showModal("Import camera photos",[
    pick("Deployment","deployment",depChoices(cat),filter.deployment_id||cat.deployments[0].id),
    pick("Capture lighting","illumination",lookups.lighting,"day-color"),
    h("label",{class:"full"},"Camera files",h("input",{name:"images",type:"file",multiple:true,accept:".jpg,.jpeg,.png,.tiff,.tif,.webp",required:true})),
    field("Camera timestamp for one file (optional)","camera_time","",{help:"Enter ISO time with an offset or let EXIF and the deployment clock resolve the order."}),
    pick("Repeated local clock hour","fold",[["0","Earlier occurrence"],["1","Later occurrence"]],"","Leave unresolved"),
    field("Import notes","note","",{wide:true,type:"area"}),
  ],async (form,values)=>{
    const response=await api(`/api/deployments/${values.deployment}/photos`,{method:"POST",body:new FormData(form)});
    filter.deployment_id=values.deployment;filter.offset=0;
    notice(`${response.new_frames} imported · ${response.duplicate_frames} duplicates.`);await refresh();
  },{confirm:"Import photos"});
}
function reviewDialog(frames,done=refresh){
  if(!frames.length){notice("Select camera photos to review.");return;}
  const first=frames[0];
  showModal(frames.length===1?"Review photo":`Review ${frames.length} photos`,[
    pick("Observation","review",lookups.review,first.review==="new"?"seen":first.review),
    choices("Animal labels","taxa",lookups.taxa,frames.length===1?first.taxa:[]),
    field("Review notes","review_note",frames.length===1?first.review_note:"",{type:"area",wide:true}),
  ],async(form,values)=>{
    values.taxa=new FormData(form).getAll("taxa");
    values.frames=frames.map(x=>x.id);values.versions=Object.fromEntries(frames.map(x=>[x.id,x.version]));
    await send("/api/frames/review",values);
    notice("Review decision recorded.");await done();
  });
}
async function frames(){
  const cat=await catalog();
  const search=new URLSearchParams(Object.entries(filter).filter(([,v])=>v!==""));
  const result=await api(`/api/frames?${search}`);
  const activeSelection=async()=>Promise.all([...selected].map(id=>api(`/api/frames/${id}`)));
  const form=h("form",{class:"filter"},pick("Deployment","deployment_id",depChoices(cat),filter.deployment_id,"All cameras"),
    pick("Review state","review",lookups.review,filter.review,"All review states"),
    pick("Lighting","illumination",lookups.lighting,filter.illumination,"Any light"),
    pick("Order","sort",[["newest","Newest capture"],["stamp","Oldest capture"],["name","Camera filename"],["recent-upload","Recently imported"]],filter.sort),
    h("button",{type:"submit"},"Apply filters"));
  form.addEventListener("submit",event=>{event.preventDefault();Object.assign(filter,data(form),{offset:0});refresh();});
  const area=h("div",{class:"actions filters"},button("Review selection",async()=>reviewDialog(await activeSelection())),
    button("Choose all shown",()=>{for(const row of result.frames)selected.add(row.id);return refresh();}),
    button("Clear selection",()=>{selected.clear();return refresh();}),
    button("Make evidence from selection",async()=>evidenceForm(await activeSelection())),
    link("Download camera register","/api/register.csv","download"));
  return [title("Camera photos","Filter the image library or open a source photo for visual review.",
    button("Import camera photos",()=>intake(cat),"primary")),
    form,area,result.frames.length?frameCards(result.frames,selected,frameOpen):empty("No photos match this view."),
    h("div",{class:"actions"},result.offset>0&&button("Previous page",()=>{filter.offset=Math.max(0,filter.offset-filter.limit);return refresh();}),
      result.more&&button("Next page",()=>{filter.offset+=filter.limit;return refresh();}))];
}

function eventProposal(cat){
  if(!cat.deployments.length){notice("Add a camera deployment first.");return;}
  const output=h("div",{class:"full"});
  let record;
  const dialog=showModal("Find trigger groups",[
    pick("Deployment","deployment_id",depChoices(cat),filter.deployment_id||cat.deployments[0].id),
    field("Separate groups after seconds","gap_s",45,{type:"number",min:2,max:3600}),
    button("Preview groups",async()=>{
      record=await send("/api/events/preview",data(dialog.form));
      output.replaceChildren(
        h("p",null,`${record.groups.length} candidate group(s) · ${record.held.length} photos held`),
        record.groups.length?choices("Select trigger groups","group",record.groups.map((item,i)=>[
          String(i),`${item.first} · ${item.count} photos`]),record.groups.map((_,i)=>String(i))):empty("No unassigned images with resolved camera time."),
        record.held.length&&detail("Held camera files",record.held),
      );
    }),output,
  ],async(form,values)=>{
    if(!record)throw new Error("Preview trigger groups first.");
    values.groups=new FormData(form).getAll("group").map(index=>record.groups[Number(index)].ids);
    const result=await send("/api/events/approve",values);
    notice(`${result.events.length} draft events saved.`);await refresh();
  },{confirm:"Save selected events"});
}
function eventEdit(cat,frameList,current=null){
  let initial=current?.deployment_id||filter.deployment_id||frameList[0]?.deployment_id||cat.deployments[0]?.id;
  if(!initial){notice("Add a camera deployment and camera photos first.");return;}
  const controls=h("div",{class:"full"});
  const view=()=>controls.replaceChildren(choices("Photos in this event","frames",
    frameList.filter(f=>f.deployment_id===initial).map(f=>[f.id,f.name]),
    current?.frames?.map(f=>f.id)||[...selected]));
  const deployment=pick("Camera deployment","deployment_id",depChoices(cat),initial);
  q("select",deployment).addEventListener("change",event=>{initial=event.target.value;view();});
  view();
  showModal(current?"Edit trigger event":"Create trigger event",[
    field("Event title","name",current?.name||"",{required:true}),deployment,
    pick("Review status","state",[["draft","Draft"],["confirmed","Confirmed"],["deferred","Deferred"]],current?.state||"draft"),
    field("Caption","caption",current?.caption||"",{wide:true,type:"area"}),controls,
  ],async(form,values)=>{
    values.frames=new FormData(form).getAll("frames");if(current)values.version=current.version;
    const item=await send(current?`/api/events/${current.id}`:"/api/events",values,current?"PUT":"POST");
    notice("Trigger event saved.");route("event",item.id);
    if(current)await refresh();
  });
}
async function events(){
  const cat=await catalog();
  const result=await api("/api/events");
  return [
    title("Trigger events","Review bursts as events while retaining the camera time and state of every frame.",
      button("Find trigger groups",()=>eventProposal(cat),"primary"),
      button("New event",async()=>eventEdit(cat,await getFrameList()))),
    h("div",{class:"grid cards"},result.events.map(item=>pane(item.name,
      item.thumbnail_id&&img(file(item.thumbnail_id,"thumbnail"),"First camera photo in event","thumbnail"),
      h("div",{class:"actions"},badge(item.state),badge(item.review_coverage.conclusion),
        h("small",null,`${item.frame_count} frames · ${item.deployment.name}`)),
      h("p",{class:"muted"},item.stamp_utc||"Clock unresolved"),recordLink("Open trigger event","event",item.id)))),
    !result.events.length&&empty("No saved events. Find trigger groups from a deployment with dated camera photos."),
  ];
}
function stripDialog(event){
  const preview=h("div",{class:"full"});
  let objectURL;
  const dialog=showModal("Build event strip",[
    field("Strip title","name",event.name+" · inspection",{required:true}),
    pick("Frame selection","mode",[["spaced","Spaced through event"],["contiguous","Consecutive frames"]],"spaced"),
    field("Number of frames","count",Math.min(8,event.frames.length),{type:"number",min:2,max:20}),
    field("First frame index","first",1,{type:"number",min:1,max:event.frames.length}),
    field("Matched crop [x,y,width,height] (optional)","box","",{wide:true,help:"Use normalized coordinates from 0 to 1."}),
    check("Show stored annotations","show_marks",true),
    button("Preview strip",async()=>{const values=data(dialog.form);values.box=jsonField(values,"box",null);
      const blob=await send(`/api/events/${event.id}/strip/preview`,values);
      if(objectURL)URL.revokeObjectURL(objectURL);objectURL=URL.createObjectURL(blob);
      preview.replaceChildren(img(objectURL,"Preview of the ordered event strip"));
    }),preview,
  ],async(form,values)=>{
    values.box=jsonField(values,"box",null);values.show_marks=form.elements.show_marks.checked;
    const record=await send(`/api/events/${event.id}/strip`,values);
    notice("Inspection strip saved.");route("report",record.id);
  },{confirm:"Save strip"});
}
async function pathDialog(event){
  const data=await api(`/api/ground-planes?deployment=${event.deployment_id}`);
  if(!data.models.length){notice("Save a ground plane for this camera deployment first.");return;}
  const list=event.frames.map(f=>row(f.name,f.stamp_utc,field("Ground point [x,y]",f.id,"")));
  showModal("Trace ground positions",[
    field("Path title","name",event.name+" · ground path",{required:true}),
    pick("Ground plane","model",data.models.map(m=>[m.id,m.name]),data.models[0].id),
    pick("Direction","direction",[["unresolved","Unresolved"],["approach","Approach"],["depart","Depart"],["crossing","Crossing"]],"unresolved"),
    h("div",{class:"full"},h("p",{class:"muted"},"Enter the visible ground contact in two or more frames. Coordinates are normalized."),list),
    field("Field notes","note","",{type:"area",wide:true}),
  ],async(_,values)=>{
    values.event=event.id;values.stations=event.frames.filter(f=>values[f.id]).map(f=>({frame:f.id,point:jsonField(values,f.id)}));
    await send("/api/event-paths",values);notice("Ground trace saved.");await refresh();
  });
}
async function oneEvent(key){
  const event=await api(`/api/events/${key}`), cat=await catalog();
  return [title(event.name,`${event.deployment.name} · ${event.frame_count} photos`,
    button("Build event strip",()=>stripDialog(event),"primary"),button("Trace ground positions",()=>pathDialog(event)),
    button("Edit event",async()=>eventEdit(cat,await getFrameList(),event)),
    button("Make evidence",()=>evidenceForm(event.frames))),
    h("section",{class:"stats"},metric("Review conclusion",event.review_coverage.conclusion),
      metric("Pending frames",event.review_coverage.pending),metric("Elapsed seconds",event.time_span.duration_s??"—")),
    event.caption&&pane("Event note",h("p",null,event.caption)),
    frameCards(event.frames,selected,frameOpen),
    ...event.paths.map(x=>pane(x.name,definition([["Supported segments",x.support_segments],["Supported ground length",`${num(x.total_supported_m)} m`],
      ["Resolved intervals",x.time_resolved]]),detail("Ground trace evidence",x.steps))),
  ];
}
function identityForm(current=null){
  const existing=current||{};
  showModal(current?"Edit individual":"Add individual reference",[
    field("Individual label","name",existing.name||"",{required:true}),pick("Animal","taxon",lookups.taxa,existing.taxon||"unknown"),
    pick("Status","status",[["provisional","Provisional"],["established","Established"]],existing.status||"provisional"),
    field("Marking notes","features",existing.features||"",{type:"area",wide:true}),
  ],async(_,values)=>{
    if(current)values.version=current.version;
    const result=await send(current?`/api/identities/${current.id}`:"/api/identities",values,current?"PUT":"POST");
    route("person",result.id);if(current)await refresh();
  });
}
async function addPortrait(person,known=null){
  const frames=await getFrameList();
  if(!frames.length){notice("Import at least one camera photo first.");return;}
  const current=known?frames.find(x=>x.id===known):frames[0];
  const preview=h("div",{class:"full"});
  let objectURL;
  const dialog=showModal("Save a marking witness",[
    pick("Camera frame","frame",frames.map(x=>[x.id,x.name]),current?.id||frames[0].id),
    pick("Body view","pose",lookups.poses,"left"),field("Witness title","name",""),
    pick("Certainty","certainty",[["probable","Probable"],["observed","Observed"],["uncertain","Uncertain"]],"probable"),
    field("Marking crop [x,y,width,height]","box","[0.35,0.60,0.18,0.16]",{required:true,wide:true}),
    field("Witness note","note","",{wide:true,type:"area"}),
    button("Preview marking",async()=>{const v=data(dialog.form);v.box=jsonField(v,"box");const blob=await send(`/api/frames/${v.frame}/crop-preview`,v);
      if(objectURL)URL.revokeObjectURL(objectURL);objectURL=URL.createObjectURL(blob);preview.replaceChildren(img(objectURL,"Selected marking crop"));
    }),preview,
  ],async(_,values)=>{
    values.box=jsonField(values,"box");await send(`/api/identities/${person.id}/portraits`,values);
    notice("Marking witness saved.");route("person",person.id);await refresh();
  });
}
async function identities(){
  const {identities:items}=await api("/api/identities");
  return [title("Individual reference library","Retain comparable markings, source frames, body side and uncertainty.",
    button("Add individual",()=>identityForm(),"primary")),
    h("div",{class:"grid cards"},items.map(item=>pane(item.name,definition([
      ["Animal",human(lookups.taxa,item.taxon)],["Status",item.status],["Recorded witnesses",item.portrait_count]]),
      recordLink("Open individual","person",item.id)))),
    !items.length&&empty("No individual records. Use a marked camera photo to start a reference.")];
}
async function oneIdentity(key){
  const item=await api(`/api/identities/${key}`);
  const catalogue=await api("/api/portraits");
  const compareDialog=(left)=>showModal("Compare marking witnesses",[
    pick("Second witness","second",catalogue.portraits.filter(r=>r.id!==left.id).map(r=>[r.id,r.name]),"","Choose a witness"),
  ],async(_,values)=>{
    if(!values.second)throw new Error("Choose a second witness.");
    const view=img(`/api/portraits/compare.png?first=${left.id}&second=${values.second}`,"Comparison of two saved marking witnesses");
    setTimeout(()=>showModal("Marking comparison",[h("div",{class:"full"},view)],()=>true,{confirm:"Done"}),0);
  },{confirm:"Compare"});
  return [title(item.name,`${human(lookups.taxa,item.taxon)} · ${item.status}`,
    button("Add marking witness",()=>addPortrait(item),"primary"),button("Edit individual",()=>identityForm(item))),
    item.features&&pane("Marking notes",h("p",null,item.features)),
    h("div",{class:"grid cards"},item.portraits.map(p=>pane(p.name,img(file(p.id,"guide"),"Saved marking crop"),
      definition([["Body view",human(lookups.poses,p.pose)],["Visible evidence",pct(p.quality.clear_fraction)],["Certainty",p.certainty]]),
      h("div",{class:"actions"},recordLink("Open source photo","frame",p.frame_id),
        catalogue.portraits.length>1&&button("Compare",()=>compareDialog(p)))))),
    !item.portraits.length&&empty("No saved marking witnesses.")];
}

function dates(){
  const d=new Date();const old=new Date();old.setDate(d.getDate()-7);
  return [old.toISOString().slice(0,10),d.toISOString().slice(0,10)];
}
function surveyFields(cat,type,training){
  const days=training?["2026-09-16","2026-09-20"]:dates();
  const paired=type==="pairing";
  return [
    field("Report title","name",type==="activity"?"Camera activity by local clock":paired?"Paired survey effort":"Survey effort",{required:true}),
    pick("Animal filter","taxon",lookups.taxa,"","Any animal"),
    field("First day","first_day",days[0],{type:"date",required:true}),
    field("Last day","last_day",days[1],{type:"date",required:true}),
    ...(paired?[]:[pick("Survey window","mode",[["active","All active camera hours"],["day-window","Daytime window"],["night-window","Night window"]],"active")]),
    type==="activity"?pick("Bucket duration","bucket_minutes",[["30","30 minutes"],["60","One hour"],["120","Two hours"]],"60"):
      field("Day begins at hour","day_from",6,{type:"number",min:0,max:22}),
    field("Day ends at hour","day_to",18,{type:"number",min:1,max:23}),
    choices("Survey stations","stations",stationChoices(cat),paired?cat.stations.slice(0,2).map(x=>x.id):cat.stations.map(x=>x.id)),
  ];
}
function saveSurvey(cat,type,training){
  if(type==="pairing"&&cat.stations.length<2){notice("Add two stations to compare paired effort.");return;}
  if(!cat.stations.length){notice("Add at least one station.");return;}
  const preview=h("div",{class:"full"});
  const get=(form)=>({...data(form),stations:new FormData(form).getAll("stations")});
  const dialog=showModal(type==="activity"?"Build clock activity chart":type==="pairing"?"Compare hours recorded at both stations":"Summarize surveillance effort",[
    ...surveyFields(cat,type,training),
    button("Preview report",async()=>{
      const r=await send(`/api/${type==="pairing"?"pairing":type}`,get(dialog.form));
      preview.replaceChildren(surveyTable(r,type));
    }),preview,
  ],async(form)=>{
    const result=await send(`/api/${type==="pairing"?"pairing":type}/save`,get(form));
    notice("Survey report saved.");route("report",result.id);
  },{confirm:"Save report"});
}
function pairingTable(r){
  const blocks=[];
  for(const section of r.classes){
    blocks.push(h("h3",null,`${section.label} · ${section.window}`),
      table(["Station","Paired hours","Seen","Not seen","Pending","Encounters per 24 h","Hours without a partner"],
        section.stations.map(item=>[item.name,num(item.paired_hours),item.seen,item.not_seen,item.pending,
          item.rate_per_24h==null?"— (no paired hours)":num(item.rate_per_24h),num(item.unpaired_hours)])));
    for(const item of section.stations){
      const other=section.stations.find(other=>other.station_id!==item.station_id);
      if(!item.unpaired_blocks.length)continue;
      blocks.push(h("p",{class:"muted"},`${item.name} alone: hours recorded when ${other.name} was not available in this window. Shown beside the comparison; not treated as absence at ${other.name}.`),
        table(["From","To","Hours","Partner unavailable","Seen","Not seen","Pending"],
          item.unpaired_blocks.map(b=>[b.start_local,b.end_local,num(b.hours),
            b.partner_unavailable.length?b.partner_unavailable.join(", "):"—",b.seen,b.not_seen,b.pending])),
        item.unpaired_blocks_truncated&&h("p",{class:"muted"},`Only the first ${item.unpaired_blocks.length} separate blocks are listed.`),
        item.unpaired_short_gap_hours>0&&h("p",{class:"muted"},
          `A further ${num(item.unpaired_short_gap_hours)} h in gaps shorter than 15 minutes is included in the hours above but not listed as blocks.`));
    }
  }
  return h("div",{class:"stacked"},...blocks);
}
function surveyTable(r,type){
  if(type==="activity")return table(["Station","Local time zone","Active hours","Positive events","Activity centre in minutes"],
    r.stations.map(item=>[item.name,item.timezone,num(item.hours),item.activity.positive_events,num(item.activity.centre_minutes)]));
  if(type==="pairing")return pairingTable(r);
  return table(["Station","Active hours","Reviewed positives","Pending","Encounters per 24 hours"],
    r.stations.map(item=>[item.station_name,num(item.hours_active),item.outcomes.seen||0,item.outcomes.pending||0,num(item.rate_per_24h)]));
}
function sampleDialog(cat){
  const preview=h("div",{class:"full"});
  const selection=(form)=>({...data(form),deployments:new FormData(form).getAll("deployments"),only_resolved:form.elements.only_resolved.checked});
  const dialog=showModal("Build a photo review sample",[
    field("Worklist title","name","Survey review sample",{required:true}),
    pick("Sample groups","stratify",lookups.strata,"camera-and-light"),
    field("Photos per group","count",3,{type:"number",min:1,max:18}),
    field("Repeatable seed","seed",73,{type:"number",min:0}),
    pick("Review filter","review",lookups.review,"","All review states"),
    check("Use only photos with resolved time","only_resolved",false),
    choices("Camera deployments","deployments",depChoices(cat),cat.deployments.map(x=>x.id)),
    button("Preview worklist",async()=>{
      const result=await send("/api/samples/preview",selection(dialog.form));
      preview.replaceChildren(table(["Group","Eligible","Selected","Shortfall"],result.strata.map(r=>[r.name,r.available,r.chosen,r.shortfall])));
    }),preview,
  ],async(form)=>{
    const item=await send("/api/samples",selection(form));
    notice("Sample worklist saved.");route("sample",item.id);
  });
}
async function surveys(){
  const [out,lists,cat,home]=await Promise.all([api("/api/reports"),api("/api/samples"),catalog(),api("/api/home")]);
  return [title("Survey reports","Compare reviewed encounters to active camera time and retain reproducible review samples.",
    button("Summarize effort",()=>saveSurvey(cat,"effort",home.training),"primary"),
    button("Compare paired effort",()=>saveSurvey(cat,"pairing",home.training)),
    button("Clock activity chart",()=>saveSurvey(cat,"activity",home.training)),
    button("Build review sample",()=>sampleDialog(cat))),
    h("div",{class:"split"},pane("Saved reports",out.reports.length?out.reports.map(r=>row(r.name,r.kind,
      recordLink("Open report","report",r.id),link("CSV",`/api/reports/${r.id}.csv`))):empty("No survey reports saved.")),
    pane("Photo sample worklists",lists.samples.length?lists.samples.map(r=>row(r.name,r.created_at,recordLink("Open worklist","sample",r.id))):
      empty("No sample worklists yet.")))];
}
async function oneReport(key){
  const record=await api(`/api/records/reports/${key}`);
  const report=record.report;
  const supporting=record.kind==="sequence"?
    pane("Sequence sources",table(["Photo","Trigger index","Review","Time"],report.metadata.map(r=>[
      recordLink(r.name,"frame",r.frame_id),r.sequence_index,r.review,r.time.utc]))):null;
  const paired=record.kind==="pairing"?
    pane("Paired comparison",h("p",{class:"muted"},report.note),pairingTable(report),
      detail("Report settings",report.settings)):null;
  return [title(record.name,record.kind==="sequence"?"Saved camera event strip":report.note||"Saved survey report",
    link("Download CSV",`/api/reports/${key}.csv`,"download"),link("All reports","#surveys","download")),
    record.assets.some(x=>x.label==="guide")&&pane(null,img(file(key,"guide"),`Saved ${record.kind} chart`)),
    paired||(record.kind==="sequence"?supporting:pane("Station results",surveyTable(report,record.kind),
      ...(report.stations||[]).map(station=>detail(station.station_name||station.name,
        record.kind==="activity"?station.entries:station.days))),
      detail("Report settings",report.settings||report.box))];
}
async function oneSample(key){
  const list=await api(`/api/records/samples/${key}`);
  const decide=(r)=>showModal("Review sample entry",[
    pick("Worklist decision","decision",[["pending","Pending"],["reviewed","Reviewed"],["deferred","Deferred"],["excluded","Excluded"]],r.decision),
    field("Review note","note",r.review_note,{type:"area",wide:true}),
  ],async(_,values)=>{
    await send(`/api/samples/${key}/review`,{...values,frame:r.id,version:list.version},"PUT");
    notice("Worklist decision saved.");await refresh();
  });
  return [title(list.name,`${list.selected_count} selected photos from ${list.eligible_count} eligible frames`),
    h("div",{class:"stats"},Object.entries(list.counts).map(([label,value])=>metric(label,value))),
    pane("Frozen sample",table(["Photo","Source state","Decision","Action"],list.members.map(r=>[
      recordLink(r.name,"frame",r.id),r.source_review,r.decision,button("Review entry",()=>decide(r))])),
      detail("Sampling settings",list.settings))];
}
async function evidenceForm(preselected=[]){
  const frames=preselected.length?preselected:await getFrameList();
  if(!frames.length){notice("Import photos before preparing a contact sheet.");return;}
  const selection=frames.slice(0,32);
  const result=h("div",{class:"full"});
  let objectURL;
  const inputs=(form)=>({...data(form),frames:new FormData(form).getAll("frames"),show_marks:form.elements.show_marks.checked,
    share:form.elements.share.checked,landscape:form.elements.landscape.checked});
  const overlay=showModal("Prepare inspection evidence",[
    field("Evidence title","name","Camera inspection",{required:true}),
    pick("Output","output",[["contact","Printable contact sheet"],["packets","Portable ZIP bundle"]],"contact"),
    pick("Paper","paper",[["a4","A4"],["letter","Letter"]],"a4"),
    pick("Layout","layout",[["two","Four photos per page"],["grid","Six photos per page"],["wide","Three wide panels"]],"two"),
    pick("Photo order","order",[["selection","Current selection"],["time","Resolved camera time"]],"time"),
    check("Landscape paper","landscape",false),check("Use privacy masks for sharing","share",true),check("Include annotations","show_marks",true),
    choices("Photos to include","frames",selection.map(x=>[x.id,x.name]),preselected.length?selection.map(x=>x.id):[selection[0].id]),
    button("Preview first page",async()=>{const blob=await send("/api/contact/preview",inputs(overlay.form));
      if(objectURL)URL.revokeObjectURL(objectURL);objectURL=URL.createObjectURL(blob);result.replaceChildren(img(objectURL,"Preview of first contact sheet page"));
    }),result,
  ],async(form,values)=>{
    const saved=await send(`/api/${values.output}`,inputs(form));
    notice("Evidence saved locally.");route("packet",saved.id);
  },{confirm:"Save evidence"});
}
function receivePacket(){
  showModal("Verify received evidence ZIP",[
    h("label",{class:"full"},"Local ZIP bundle",h("input",{type:"file",name:"archive",accept:".zip",required:true})),
  ],async(form)=>{
    const item=await api("/api/packets/receive",{method:"POST",body:new FormData(form)});
    notice(item.duplicate?"The verified packet is already in this survey.":"Local bundle digests and image entries verified.");
    route("packet",item.id);
  },{confirm:"Verify bundle"});
}
async function evidence(){
  const {packets}=await api("/api/packets");
  return [title("Inspection evidence","Keep local printable contact sheets and verified copies of exchanged camera evidence.",
    button("Prepare evidence",()=>evidenceForm(),"primary"),button("Verify evidence ZIP",receivePacket)),
    pane("Evidence library",packets.length?packets.map(r=>row(r.name,`${r.kind} · ${r.frame_count} photos`,recordLink("Open evidence","packet",r.id))):
      empty("No evidence saved. Select photos from a camera or event."))];
}
async function onePacket(key){
  const item=await api(`/api/packets/${key}`);
  const have=(label)=>item.assets.some(asset=>asset.label===label);
  const integrity=h("div");
  return [title(item.name,`${item.kind} · ${item.frames.length} camera frames`,
    have("pdf")&&link("Download PDF",file(key,"pdf"),"download primary"),
    have("packet")&&link("Download ZIP",file(key,"packet"),"download"),
    have("manifest")&&link("Source manifest",file(key,"manifest"),"download"),
    button("Verify local attachments",async()=>{
      const result=await api(`/api/attachment-check/${key}?sha256=true`);
      integrity.replaceChildren(pane("Attachment verification",table(["File","State","Bytes"],result.assets.map(r=>[r.label,r.state,r.bytes]))));
    })),
    integrity,have("contact")&&pane(null,img(file(key,"contact"),"First inspection contact sheet")),
    pane("Photo index",table(["Photo","Station","Lighting","UTC","Evidence"],item.frames.map((r,i)=>[
      r.name,r.site,r.illumination,r.time_utc,item.kind==="received"?
        link("Inspection image",file(key,`image-${String(i+1).padStart(3,"0")}`),"download"):
        recordLink("Open local source","frame",r.frame_ref)])),item.status==="verified"&&badge("Verified bundle"))];
}
async function render(){
  const ticket=++routeRun;
  const [section,key]=routeState();
  navigation(section);
  workspace.dataset.page=section;
  try{
    const acts={home,stations,events,frames,identities,surveys,evidence,event:oneEvent,person:oneIdentity,
      packet:onePacket,report:oneReport,sample:oneSample,
      frame:(id)=>frameWorkbench(id,{reviewDialog,refresh,evidenceForm,addPortrait,getFrameList,frameOpen})};
    const output=await (acts[section]||home)(key);
    if(ticket===routeRun)workspace.replaceChildren(...output.filter(Boolean));
  }catch(error){
    if(ticket===routeRun)workspace.replaceChildren(title("Could not open this view",error.message),link("Return to overview","#home"));
  }
}
window.addEventListener("hashchange",()=>{render();window.scrollTo({top:0,behavior:"instant"});});
async function start(){
  try{setLookups(await api("/api/options"));await render();}
  catch(error){workspace.replaceChildren(title("Workspace unavailable",error.message));}
}
start();
