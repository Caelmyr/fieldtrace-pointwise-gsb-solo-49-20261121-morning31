import {
  api,badge,button,check,data,definition,detail,empty,field,file,h,human,img,jsonField,link,list,
  lookups,notice,num,pane,pct,pick,q,recordLink,row,send,showModal,stamp,table,title,
} from "./ui.js";

const PREFIX="/api/frames";
const FULL= [0,0,1,1];
const DEFAULT_PATCH=[.31,.57,.34,.24];
const boxText=(box)=>JSON.stringify(box);
const thumbnails=(name,records)=>h("div",{class:"actions"},records.map(f=>recordLink(f.name,"frame",f.id)));
const reportAsset=(kind)=>["motion","quality"].includes(kind)?kind:kind==="exposure-assist"?"assist":"guide";
const normalBox=()=>field("Source window [x,y,width,height]","box",boxText(DEFAULT_PATCH),{wide:true,help:"Enter normalized source coordinates."});

function tileAtPoint(report,event,element){
  const r=element.getBoundingClientRect();
  // object-fit: contain letterboxes the image inside its element box; map into the content box.
  const scale=Math.min(r.width/report.extent[0],r.height/report.extent[1]);
  const drawW=report.extent[0]*scale, drawH=report.extent[1]*scale;
  const insetX=(r.width-drawW)/2, insetY=(r.height-drawH)/2;
  const x=(event.clientX-r.left-insetX)/scale;
  const y=(event.clientY-r.top-insetY)/scale;
  if(x<0||y<0||x>=report.extent[0]||y>=report.extent[1])return null;
  return report.tiles.find(t=>x>=t.box[0]&&x<t.box[0]+t.box[2]&&y>=t.box[1]&&y<t.box[1]+t.box[3]);
}
const tile_target_pct=(tile)=>(tile.target_readable_fraction??0)*100;
function exposureRecordModal(item){
  const report=item.report||{};
  const summary=report.summary||{};
  const tone=report.tone_match||{};
  const tileRows=(report.tiles||[]).filter(t=>t.source!=="target").map(t=>[
    `#${t.index}`,t.source,t.reason,`${Math.round(t.target_readable_fraction*100)}%`,
    `${Math.round(t.reference_readable_fraction*100)}%`,t.conflict_px||"",
  ]);
  const conflictRows=(report.conflicts||[]).map((c,i)=>[
    i+1,c.area_px,`${Math.round(c.centroid[0]*100)}%, ${Math.round(c.centroid[1]*100)}%`,
    c.box.join(", "),
  ]);
  const assist=img(file(item.id,"assist"),"Exposure assist composite with conflict and unread tiles marked");
  const readout=h("p",{class:"help"},"Click the assist image to identify which frame each tile was read from.");
  assist.style.cursor="crosshair";
  assist.addEventListener("click",event=>{
    const tile=tileAtPoint(report,event,assist);
    if(!tile)return;
    readout.textContent=`Tile #${tile.index} · source: ${tile.source} · reason: ${tile.reason} · target readable ${Math.round(tile_target_pct(tile))} / reference readable ${Math.round(tile.reference_readable_fraction*100)}% · box ${tile.box.join(", ")}`;
  });
  const counts=summary.tiles_by_source||{};
  showModal(item.name,[h("div",{class:"full stacked"},
    assist,readout,
    definition([
      ["Comparison state",report.comparison_state],
      ["Tiles from current frame",counts.target],["Tiles from reference frame",counts.reference],
      ["Conflict tiles",counts.conflict],["Unread tiles",counts.unread],
      ["Conflict components",summary.conflict_components],
      ["Conflict fraction",summary.conflict_fraction_of_common!=null?pct(summary.conflict_fraction_of_common):null],
      ["Jointly readable pixels",summary.jointly_readable_pixels],
      ["Tone match scale",tone.scale],["Tone samples",tone.common_readable_pixels],
    ]),
    img(file(item.id,"provenance"),"Tile provenance map"),
    conflictRows.length&&table(["#","Area px","Centroid","Box [x,y,w,h]"],conflictRows),
    table(["Tile","Source","Reason","Target readable","Reference readable","Conflict px"],tileRows),
    detail("Method and complete evidence",report))],()=>true,{confirm:"Done"});
}
function recordModal(item){
  if(item.kind==="exposure-assist"){exposureRecordModal(item);return;}
  const report=item.report||{};
  const summary=[
    ["Operation",item.kind],["Image support",report.support?.fraction!=null?pct(report.support.fraction):null],
    ["Usable detail",report.usable_detail_rate!=null?pct(report.usable_detail_rate):null],
    ["Change candidates",report.review_candidates?.length],
    ["Decision",report.comparison_state||report.conclusion||report.state],
    ["Profile stations",report.station_count],
    ["Stations measured",report.supported],
    ["Dominant direction",report.orientation_deg!=null?`${num(report.orientation_deg)}°`:null],
    ["Jointly visible samples",report.support?.joint],
  ].filter(([,v])=>v!==null&&v!==undefined);
  showModal(item.name,[h("div",{class:"full stacked"},
    img(file(item.id,reportAsset(item.kind)),`${item.kind} inspection result`),
    definition(summary),report.categories&&table(["Visibility category","Pixel count"],Object.entries(report.categories)),
    report.review_candidates?.length>0&&table(["Candidate","Area","Median change"],report.review_candidates.map((x,i)=>[i+1,x.area_px,num(x.median_change,3)])),
    detail("Complete inspection evidence",report))],()=>true,{confirm:"Done"});
}
async function finishRecord(item,refresh){
  notice("Inspection result saved.");
  await refresh();
  setTimeout(()=>recordModal(item),0);
}
function createMark(frame,points,refresh,existing=null){
  const initial=existing||{};
  showModal(existing?"Edit source outline":"Draw source outline",[
    field("Outline name","name",initial.name||"",{required:true}),
    pick("Outline category","kind",lookups.marks,initial.kind||"animal"),
    pick("Animal label","taxon",lookups.taxa,initial.taxon||"unknown"),
    pick("Certainty","certainty",[["observed","Observed"],["probable","Probable"],["uncertain","Uncertain"]],initial.certainty||"observed"),
    field("Vertices [[x,y],…]","polygon",JSON.stringify(existing?.polygon||points||[]),{type:"area",wide:true,required:true}),
    field("Note","note",initial.note||"",{type:"area",wide:true}),
  ],async(_,values)=>{
    values.polygon=jsonField(values,"polygon");
    if(existing)values.version=existing.version;
    const root=`${PREFIX}/${frame.id}/marks`;
    await send(existing?`${root}/${existing.id}`:root,values,existing?"PUT":"POST");
    notice("Source outline saved.");await refresh();
  });
}
function reviseTime(frame,refresh){
  const form=[
    pick("Time evidence","mode",[["clock","Camera local clock"],["utc","Direct UTC or offset time"],["unresolved","Hold as unresolved"]],"clock"),
    field("Camera stamp or ISO time","stamp",frame.time.camera_time?.replace(/^(\d{4}):(\d\d):(\d\d) /,"$1-$2-$3T")||frame.stamp_utc||"",{wide:true}),
    pick("If the hour repeats","fold",[["0","Earlier occurrence"],["1","Later occurrence"]],"","Leave unresolved"),
    field("Uncertainty in seconds","uncertainty_s",frame.time.uncertainty_s||0,{type:"number",min:0}),
  ];
  showModal("Review camera time",form,async (_,values)=>{
    if(values.fold==="")delete values.fold;
    await send(`${PREFIX}/${frame.id}/time`,{...values,version:frame.version},"PUT");
    notice("Camera time status saved.");await refresh();
  });
}
function qualityDialog(frame,refresh){
  showModal("Map image quality",[
    field("Audit title","name","Image quality map",{required:true}),
    field("Shadow limit","shadows",.08,{type:"number",min:.005,max:.35,step:.005}),
    field("Highlight limit","highlights",.975,{type:"number",min:.65,max:1,step:.005}),
    field("Minimum edge detail","minimum_detail",.018,{type:"number",min:.001,max:.3,step:.001}),
    field("Grid rows","rows",5,{type:"number",min:2,max:16}),
    field("Grid columns","columns",7,{type:"number",min:2,max:20}),
    field("Crop box (optional)","box","",{wide:true,help:"Enter [x,y,width,height] using normalized coordinates."}),
  ],async(_,values)=>{
    values.box=jsonField(values,"box",null);
    const item=await send(`${PREFIX}/${frame.id}/quality`,values);await finishRecord(item,refresh);
  },{confirm:"Save quality map"});
}
async function comparisonDialog(frame,refresh,mode){
  const all=await api(`/api/frames?limit=160&deployment_id=${frame.deployment_id}`);
  const candidates=all.frames.filter(f=>f.id!==frame.id&&f.extent.join()===frame.extent.join()&&
    (mode!=="motion"||f.illumination===frame.illumination));
  if(!candidates.length){notice("No comparable viewing photos in this camera deployment.");return;}
  const label=mode==="motion"?"Find movement candidates":mode==="placement"?"Audit camera placement":"Compare camera windows";
  const common=[field("Record title","name",label,{required:true}),pick("Reference photo","reference",candidates.map(a=>[a.id,a.name]),candidates[0].id)];
  const anchors=[{name:"Far left",before:[.23,.59],after:[.23,.59]},
    {name:"Far right",before:[.70,.56],after:[.70,.56]},
    {name:"Near marker",before:[.49,.79],after:[.49,.79]}];
  const extra=mode==="motion"?[
    field("Minimum local change","threshold",.085,{type:"number",step:.005,min:.01,max:.4}),
    field("Smallest candidate area","min_area",60,{type:"number",min:8,max:4000}),
  ]:mode==="placement"?[
    field("Matching landmarks","anchors",JSON.stringify(anchors,null,2),{type:"area",wide:true}),
    field("Allowed displacement (px)","allowed_shift_px",5,{type:"number",step:.5,min:.5}),
    field("Allowed landmark spread (px)","allowed_spread_px",4,{type:"number",step:.5,min:.5}),
  ]:[
    pick("View style","view",[["two","Side by side"],["wipe","Split at centre"],["alternating","Alternating bands"]],"two"),
    pick("Brightness","brightness",[["color","Ordinary image"],["common-window","Shared luminance window"]],"color"),
    field("Common crop (optional)","box","",{wide:true,help:"Use [x,y,width,height] between 0 and 1."}),
  ];
  showModal(label,[...common,...extra],async(_,values)=>{
    if(mode==="placement")values.anchors=jsonField(values,"anchors");
    if(mode==="window-compare")values.box=jsonField(values,"box",null);
    const result=await send(`${PREFIX}/${frame.id}/${mode}`,values);await finishRecord(result,refresh);
  },{confirm:"Inspect and save"});
}
function exposureAssistDialog(frame,refresh){
  api(`/api/frames?limit=160&deployment_id=${frame.deployment_id}`).then(all=>{
    const candidates=all.frames.filter(f=>f.id!==frame.id&&f.extent.join()===frame.extent.join());
    if(!candidates.length){notice("No same-framing photos in this camera deployment to pair with.");return;}
    showModal("Build exposure assist view",[
      field("Assist title","name","Exposure assist view",{required:true}),
      pick("Reference photo (the other exposure)","reference",candidates.map(a=>[a.id,a.name]),candidates[0].id),
      field("Readable shadow limit","shadows",.06,{type:"number",step:.005,min:.005,max:.35}),
      field("Readable highlight limit","highlights",.97,{type:"number",step:.005,min:.65,max:1}),
      field("Tile size (px)","tile_px",24,{type:"number",min:8,max:120}),
      field("Minimum readable share per tile","readable_fraction",.5,{type:"number",step:.05,min:.2,max:.95}),
      field("Outline edge threshold","edge_threshold",.07,{type:"number",step:.005,min:.02,max:.3}),
      field("Smallest conflict area (px)","min_conflict_area",48,{type:"number",min:8,max:4000}),
      field("Widespread conflict limit","conflict_area_limit",.25,{type:"number",step:.01,min:.03,max:.9}),
      check("Apply ignore and obstruction marks","use_exclusions",true),
      h("p",{class:"help full"},"Tiles are taken only from a frame that is independently readable. Outline disagreement is held out as a conflict instead of merged; areas clipped or unsupported in both stay unread."),
    ],async(form,values)=>{
      values.use_exclusions=form.elements.use_exclusions.checked;
      const item=await send(`${PREFIX}/${frame.id}/exposure-assist`,values);
      await finishRecord(item,refresh);
    },{confirm:"Build assist view"});
  });
}
function localTrace(frame,refresh,points=null){
  showModal("Measure image profile",[
    field("Profile title","name","Camera image profile",{required:true}),
    pick("Channels","channels",[["luminance","Luminance"],["rgb","RGB"]],"luminance"),
    field("Profile line vertices","points",JSON.stringify(points||[[.32,.63],[.75,.64]]),{wide:true,required:true}),
    field("Sample stride (px)","interval_px",3,{type:"number",min:.5,max:80,step:.5}),
    field("Stripe width (px)","stripe_width",5,{type:"number",min:1,max:80}),
  ],async(_,values)=>{
    values.points=jsonField(values,"points");
    const item=await send(`${PREFIX}/${frame.id}/profile`,values);await finishRecord(item,refresh);
  },{confirm:"Measure profile"});
}
function textureDialog(frame,refresh){
  showModal("Inspect marking pattern",[
    field("Pattern title","name","Marking texture",{required:true}),
    normalBox(),
    field("Minimum edge strength","minimum_strength",.018,{type:"number",step:.001,min:.002,max:.3}),
    field("Maximum repeat lag in pixels","max_shift",28,{type:"number",min:4,max:110}),
  ],async(_,values)=>{
    values.box=jsonField(values,"box");
    const result=await send(`${PREFIX}/${frame.id}/pattern`,values);await finishRecord(result,refresh);
  },{confirm:"Save marking pattern"});
}
function groundPlane(frame,refresh){
  const example=[
    {name:"far left",image:[.34,.58],ground_m:[-1.8,5.5]},
    {name:"far right",image:[.64,.58],ground_m:[1.8,5.5]},
    {name:"near left",image:[.21,.87],ground_m:[-1.8,1]},
    {name:"near right",image:[.78,.87],ground_m:[1.8,1]},
  ];
  showModal("Calibrate a ground plane",[
    field("Ground plane title","name","Marked trail plane",{required:true}),
    field("Maximum landmark error (m)","max_error_m",.1,{type:"number",min:.005,max:3,step:.005}),
    field("Known ground and image landmarks","anchors",JSON.stringify(example,null,2),
      {type:"area",wide:true,help:"At least four points, each using normalized image coordinates and known metre coordinates."}),
    field("Note","note","",{wide:true,type:"area"}),
  ],async(_,values)=>{
    values.frame=frame.id;values.anchors=jsonField(values,"anchors");
    await send("/api/ground-planes",values);notice("Ground plane saved.");await refresh();
  });
}
function measureDialog(frame,models,refresh){
  if(!models.length){notice("Calibrate a ground plane for this deployment first.");return;}
  showModal("Measure ground distance",[
    field("Distance title","name","Ground path distance",{required:true}),
    pick("Ground plane","model",models.map(x=>[x.id,x.name]),models[0].id),
    field("Ground points in image","points","[[0.39,0.73],[0.61,0.70]]",{wide:true,required:true}),
  ],async(_,values)=>{
    values.points=jsonField(values,"points");values.frame=frame.id;
    await send("/api/ground-measures",values);notice("Ground distance saved.");await refresh();
  },{confirm:"Measure ground distance"});
}
function identityChoice(frame,onChoose){
  return api("/api/identities").then(({identities})=>{
    if(!identities.length){notice("Create an individual reference in the Individuals view.");return;}
    showModal("Add this photo as a witness",[
      pick("Individual","individual",identities.map(x=>[x.id,x.name]),identities[0].id),
    ],async(_,v)=>{
      const person=identities.find(x=>x.id===v.individual);
      setTimeout(()=>onChoose(person,frame.id),0);
    },{confirm:"Choose reference"});
  });
}
function readPixel(frame,point,output){
  return send(`${PREFIX}/${frame.id}/probe`,{point,radius:4}).then(value=>{
    const x=value.coordinate.working_px[0],y=value.coordinate.working_px[1];
    output.replaceChildren(
      h("strong",null,`${x}, ${y} px · ${value.display_state}`),
      h("small",null,`${value.count}/${value.requested} supported pixels${value.at_point_rgb? " · RGB "+value.at_point_rgb.join(", "):""}`),
    );
  });
}
function enableSketch(frame,ui,refresh){
  const {scene,canvas,lead}=ui;
  const width=frame.extent[0],height=frame.extent[1];
  let state="",points=[];
  const ctx=canvas.getContext("2d");
  const saved=()=>points.map(p=>p.map(v=>Number(v.toFixed(5))));
  function redraw(){
    ctx.clearRect(0,0,width,height);
    ctx.strokeStyle="#bbf0b8";ctx.fillStyle="#d6f7c2";ctx.lineWidth=Math.max(2,width/360);
    if(points.length){
      ctx.beginPath();
      for (let i=0;i<points.length;i++){
        const p=points[i];if(i===0)ctx.moveTo(...p.map((v,j)=>v*(j?height:width)));
        else ctx.lineTo(...p.map((v,j)=>v*(j?height:width)));
      }
      ctx.stroke();
    }
    for(const [x,y] of points){ctx.beginPath();ctx.arc(x*width,y*height,Math.max(3,width/165),0,Math.PI*2);ctx.fill();}
  }
  const set=(mode)=>{
    points=[];state=mode;scene.classList.toggle("marking",!!mode);
    lead.replaceChildren(h("p",null,mode==="probe"?"Click the image to sample source pixels.":mode==="polygon"?
      "Click three or more boundary points.":"Click the image to trace a sampling line."));
    redraw();
  };
  canvas.addEventListener("pointerdown",async event=>{
    if(!state)return;
    const r=canvas.getBoundingClientRect();
    const point=[Math.min(1,Math.max(0,(event.clientX-r.left)/r.width)),Math.min(1,Math.max(0,(event.clientY-r.top)/r.height))];
    if(state==="probe"){points=[point];redraw();try{await readPixel(frame,point,lead);}catch(exc){notice(exc.message);}return;}
    points.push(point);redraw();
    lead.replaceChildren(h("p",null,`${points.length} image point(s) selected`));
  });
  const finish=()=>{
    if(state==="polygon"&&points.length>=3)createMark(frame,saved(),refresh);
    else if(state==="line"&&points.length>=2)localTrace(frame,refresh,saved());
    else if(state!=="probe")throw new Error("Choose enough points to finish.");
  };
  return h("div",{class:"actions"},
    button("Draw outline",()=>set("polygon")),button("Trace image line",()=>set("line")),
    button("Probe image",()=>set("probe")),button("Undo point",()=>{points.pop();redraw();}),
    button("Finish sketch",finish),button("Clear sketch",()=>{set("");lead.replaceChildren();}));
}
function viewer(frame,refresh){
  const image=img(file(frame.id,"work"),`Current camera photo ${frame.name}`,"");
  const canvas=h("canvas",{width:frame.extent[0],height:frame.extent[1],"aria-hidden":"true"});
  const scene=h("div",{class:"scene"},image,canvas);
  const lead=h("figcaption",null);
  const modes=[
    ["Standard",file(frame.id,"work")],["Annotated",`${PREFIX}/${frame.id}/marked.png?v=${frame.marks.map(r=>r.version).join("-")}`],
    ["Lift shadows",`${PREFIX}/${frame.id}/display.png?mode=shadow`],
    ["Luminance window",`${PREFIX}/${frame.id}/display.png?mode=window`],
    ["Edges",`${PREFIX}/${frame.id}/display.png?mode=edge`],
  ];
  let current;
  const tools=h("div",{class:"actions"},modes.map(([label,url],i)=>{
    const trigger=button(label,()=>{
      current?.classList.remove("active");trigger.classList.add("active");current=trigger;image.src=url;
    },i===0?"active":"");
    if(i===0)current=trigger;
    return trigger;
  }));
  return h("figure",{class:"viewer",style:{margin:"0"}},scene,tools,enableSketch(frame,{canvas,scene,lead},refresh),lead);
}
function detailsPanel(frame,apiContext){
  const {refresh,reviewDialog}=apiContext;
  return pane("Camera evidence",definition([
    ["Station",frame.station.name],["Deployment",frame.deployment.name],["Capture time",stamp(frame)],
    ["Time evidence",frame.time.basis],["Clock uncertainty",frame.time.uncertainty_s==null?"—":`${frame.time.uncertainty_s} s`],
    ["Lighting",human(lookups.lighting,frame.illumination)],["Review",human(lookups.review,frame.review)],
    ["Animals",list(frame.taxa.map(x=>human(lookups.taxa,x)))],["Image support",pct(frame.supported_fraction)],
    ["Source dimensions",frame.source_extent.join(" × ")],
  ]),h("div",{class:"actions"},button("Review photo",()=>reviewDialog([frame]),"primary"),
    button("Review camera time",()=>reviseTime(frame,refresh)),
    frame.event_id&&recordLink("Open trigger event","event",frame.event_id)),
  frame.review_note&&h("p",null,frame.review_note),detail("Raw camera metadata",frame.camera));
}
async function auditHistory(frame){
  const [a,b]=await Promise.all([api(`/api/history/${frame.id}`),api(`/api/attachment-check/${frame.id}?sha256=true`)]);
  showModal("Photo revision and attachment history",[
    h("div",{class:"full"},table(["Version","Action","Time"],a.history.map(x=>[x.version,x.action,x.at])),
      table(["Attachment","Status","Bytes"],b.assets.map(x=>[x.label,x.state,x.bytes]))),
  ],()=>true,{confirm:"Done"});
}
export async function frameWorkbench(id,tools){
  const frame=await api(`${PREFIX}/${id}`);
  const [models,near]=await Promise.all([api(`/api/ground-planes?deployment=${frame.deployment_id}`),
    api(`/api/neighbours?frame=${id}&neighbours=2${frame.event_id?"&event="+frame.event_id:""}`)]);
  const refresh=tools.refresh;
  return [title(frame.name,"Inspect and record evidence at the camera photo's original position.",
    frame.origin==="synthetic"&&badge("Synthetic training photo","synthetic"),
    button("Prepare evidence",()=>tools.evidenceForm([frame])),
    link("Download original photo",file(id,"source"),"download")),
    h("div",{class:"workspace-grid"},h("div",{class:"stacked"},viewer(frame,refresh),
      pane("Nearby camera frames",h("div",{class:"actions"},near.before.length&&thumbnails("Earlier",near.before),
        badge(`Photo ${near.index??"?"} of ${near.eligible}`),near.after.length&&thumbnails("Later",near.after)))),
      h("aside",{class:"stacked"},detailsPanel(frame,tools),
      pane("Image inspections",h("div",{class:"actions"},
        button("Map image quality",()=>qualityDialog(frame,refresh)),
        button("Exposure assist pair",()=>exposureAssistDialog(frame,refresh)),
        button("Find movement candidates",()=>comparisonDialog(frame,refresh,"motion")),
        button("Compare camera windows",()=>comparisonDialog(frame,refresh,"window-compare")),
        button("Audit camera placement",()=>comparisonDialog(frame,refresh,"placement")),
        button("Measure image profile",()=>localTrace(frame,refresh)),
        button("Inspect marking pattern",()=>textureDialog(frame,refresh)),
        button("Add as identity witness",()=>identityChoice(frame,tools.addPortrait))),
      ...frame.glances.map(item=>row(item.name,item.kind,button("Open",()=>recordModal(item))))),
      pane("Source outlines",frame.marks.length?frame.marks.map(x=>row(x.name,human(lookups.marks,x.kind),
        button("Edit",()=>createMark(frame,null,refresh,x)))):empty("Draw an outline using the camera image or enter its vertices."),
        button("Enter outline coordinates",()=>createMark(frame,[],refresh))),
      pane("Ground geometry",h("div",{class:"actions"},button("Calibrate ground plane",()=>groundPlane(frame,refresh)),
        button("Measure ground distance",()=>measureDialog(frame,models.models,refresh))),
        models.models.map(model=>row(model.name,model.fit_pass?`${num(model.rms_m,3)} m fit error`:"Fit held",
          button("Open guide",()=>showModal("Ground reference guide",[
            h("div",{class:"full"},img(`/api/ground-planes/${model.id}/guide.png`,"Ground landmark support guide"))],
            ()=>true,{confirm:"Done"})))),
        frame.measures.map(value=>row(value.name,`${num(value.total_supported_m)} m in supported segments`,
          button("Details",()=>showModal("Ground measurement",[h("div",{class:"full"},detail("Track segments",value.segments))],
            ()=>true,{confirm:"Done"}))))),
      pane("Reproducibility",button("Revision and attachment history",()=>auditHistory(frame))))
    )];
}
