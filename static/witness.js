import {
  api,badge,button,empty,h,human,img,link,lookups,notice,num,pick,recordLink,row,send,showModal,table,
} from "./ui.js";

const PREFIX="/api/frames";
const AREAS=()=>Object.entries(lookups.regions||{}).filter(([key])=>key!=="unspecified");
const TONE={candidate:"seen",weak:"",mixed:"held",differs:"held",unresolved:""};
const STANDING={candidate:"Still a candidate",weak:"Weak resemblance, still a candidate",
  mixed:"Marks disagree between areas",differs:"Marked area differs in a clear view",
  unresolved:"Not enough comparable marks"};
const statusText=(status)=>status.replaceAll("-"," ");
const standingText=(row)=>row.standing_label||STANDING[row.standing]||row.standing;

function areaReading(read,frameId){
  const witness=read.witness;
  return h("div",{class:"item-row"},
    h("div",{class:"grow stacked tight"},
      h("div",{class:"actions"},h("strong",null,read.label),
        badge(statusText(read.status),TONE[read.status==="resembles"?"candidate":read.status==="differs"?"differs":""]||""),
        read.score==null?null:h("span",{class:"muted"},`resemblance ${num(read.score,2)}`)),
      witness?h("span",{class:"muted"},
        `closest retained witness: ${witness.name} · ${witness.frame_name||"photo"} · ${human(lookups.poses,witness.pose)} · ${witness.certainty||"certainty unrecorded"}`):null,
      read.note&&h("span",{class:"muted"},read.note)),
    witness?h("div",{class:"actions"},
      link("Open witness photo","#frame/"+witness.frame_id),
      button("Compare crops",()=>compareCrops(read,witness,frameId)),
      img(`/api/file/${witness.id}/guide`,`Retained witness ${witness.name}`,"thumbnail")):null);
}

function compareCrops(read,witness,frameId){
  const box=encodeURIComponent(JSON.stringify(read.box));
  showModal("Marked area beside the retained witness",[
    h("div",{class:"full"},img(`/api/witness-compare.png?frame=${frameId}&witness=${witness.id}&box=${box}`,
      `New marked area compared with ${witness.name}`)),
    h("p",{class:"full muted"},"Both panels are cut from the original photos. A close resemblance is only a reason to look at them."),
  ],()=>true,{confirm:"Done"});
}

function candidateBlock(subject,frameId){
  const unresolved=subject.regions.filter(read=>!["resembles","differs","inconclusive"].includes(read.status));
  return h("section",{class:"card stacked tight"},
    h("div",{class:"title-row"},
      h("h3",null,recordLink(subject.identity.name,"person",subject.identity.id)),
      badge(standingText(subject),TONE[subject.standing]||"")),
    subject.regions.length?table(["Marked area","Reading","Closest witness"],subject.regions.map(read=>[
      read.label,statusText(read.status),
      read.witness?recordLink(read.witness.name,"frame",read.witness.frame_id):"—"])):null,
    ...subject.regions.map(read=>areaReading(read,frameId)),
    (subject.unmarked_regions||[]).length?h("p",{class:"muted"},
      `Left unmarked here, so unresolved rather than a mismatch: `
      +`${subject.unmarked_regions.map(item=>item.label).join(", ")}. `
      +`This individual has retained witnesses there, but you did not mark that area on this photo.`):null,
    unresolved.length?h("p",{class:"muted"},
      `${unresolved.length} marked area${unresolved.length===1?"":"s"} left unresolved because this photo or its retained witnesses do not show enough there. Unresolved is not a mismatch.`):null,
    ["candidate","weak","mixed"].includes(subject.standing)
      ?h("p",{class:"muted"},"Kept as a candidate. The operator still compares the source photos; nothing here decides an identity."):null);
}

function otherSideBlock(entry){
  return h("section",{class:"card stacked tight"},
    h("div",{class:"title-row"},h("h3",null,recordLink(entry.identity.name,"person",entry.identity.id)),
      badge("Other-side witnesses only","")),
    h("p",{class:"muted"},`The retained witnesses for this individual are ${entry.poses.map(p=>human(lookups.poses,p)).join(" and ")}. They stay unassessed here rather than being read as a mismatch.`),
    h("div",{class:"actions"},entry.portraits.map(p=>link(p.name+" · "+(p.frame_name||"photo"),"#frame/"+p.frame_id))));
}

function resultBlocks(answer,{onSave}={}){
  const head=h("div",{class:"stacked tight"},
    h("p",{class:"muted"},`${answer.pose_label} · ${answer.subjects.length} individual${answer.subjects.length===1?"":"s"} with a retained witness on this side.`),
    h("div",{class:"actions"},h("span",{class:"muted"},"Marked areas:"),
      ...answer.regions.map(region=>badge(`${region.label} · ${region.status==="assessed"?"assessed":"not usable"}`,
        region.status==="assessed"?"seen":"held"))),
    h("ul",{class:"muted"},answer.notes.map(text=>h("li",null,text))));
  const blocks=answer.subjects.map(subject=>candidateBlock(subject,answer.frame.id));
  return [head,
    blocks.length?h("div",{class:"stacked"},...blocks)
      :empty("No individual has a retained witness on this side for the areas you marked. Nothing has been ruled out."),
    answer.other_side.length?h("section",{class:"stacked tight"},
      h("h2",null,"Retained from the other side"),
      ...answer.other_side.map(otherSideBlock)):null,
    h("p",{class:"muted"},
      `${answer.without_witness} individual${answer.without_witness===1?"":"s"} in this filter have no retained witnesses yet.`),
    onSave?h("div",{class:"actions"},button("Save this search",onSave,"primary")):null,
  ].filter(Boolean);
}

function searchDialog(frame,refresh,{preset=null}={}){
  const resultArea=h("div",{class:"full stacked"});
  const sketchHolder=h("div",{class:"full stacked tight"});
  const choose=pick("Body side shown here","pose",[["left","Left flank"],["right","Right flank"]],
    preset?.pose||"left");
  const taxon=pick("Animal","taxon",lookups.taxa,preset?.taxon||"fox","Any animal");
  const minimum=h("label",null,"Minimum resemblance to keep a candidate",
    h("input",{type:"number",name:"minimum",value:String(Math.round((preset?.minimum??0.3)*100)),min:"0",max:"100"}),
    h("span",{class:"help"},"How close the marked areas must look, from 0 (identical) to 100 (very loose). It only decides what to keep as a candidate."));
  const areas=AREAS().map(([key,title])=>{
    const chosen=(preset?.regions||[]).find(item=>item.region===key);
    const row=h("div",{class:"item-row"},
      h("label",{class:"check"},h("input",{type:"checkbox",name:"area-"+key,checked:!!chosen}),title),
      h("p",{class:"muted box-reading"},chosen?`Saved area [x, y, width, height]: ${chosen.box.join(", ")}`:"No area drawn."),
      h("div",{class:"actions"},
        button("Draw on photo",()=>draw(key,row)),
        button("Clear",()=>{row.dataset.box="";row.querySelector("input").checked=false;
          row.querySelector(".box-reading").textContent="No area drawn.";})));
    row.dataset.region=key;
    row.dataset.box=chosen?JSON.stringify(chosen.box):"";
    return row;
  });
  function draw(key,row){
    sketchHolder.replaceChildren(
      h("p",{class:"muted"},`Drag a box around the ${human(lookups.regions,key).toLowerCase()} on the photo.`),
      boxSketch(frame,{onBox:(box)=>{
        row.dataset.box=JSON.stringify(box);
        row.querySelector("input").checked=true;
        row.querySelector(".box-reading").textContent=`Source box [x, y, width, height]: ${box.join(", ")}`;
      }}));
    row.querySelector("input").checked=true;
  }
  const fields=[
    h("div",{class:"form-grid"},choose,taxon,minimum),
    h("div",{class:"full stacked tight"},
      h("p",null,"Tick the parts this photo shows, then draw a box around each one. Areas you leave unticked are treated as not photographed here, not as a mismatch."),
      h("div",{class:"stacked tight"},areas)),
    sketchHolder,resultArea];
  const collect=(elements)=>{
    const values={pose:elements.pose.value,taxon:elements.taxon.value||undefined,
      minimum:Number(elements.minimum.value||30)/100,regions:[]};
    for(const entry of areas){
      if(!entry.querySelector("input").checked)continue;
      try{const box=entry.dataset.box?JSON.parse(entry.dataset.box):null;
        if(box)values.regions.push({region:entry.dataset.region,box});}catch{}
    }
    return values;
  };
  let saved=false;
  const dialog=showModal("Find retained witnesses for this photo",fields,async(form)=>{
    const values=collect(form.elements);
    if(!values.regions.length)throw new Error("Mark at least one area this photo shows.");
    const answer=await send(`${PREFIX}/${frame.id}/witness-search`,values);
    resultArea.replaceChildren(...resultBlocks(answer,{onSave:async()=>{
      if(saved)return;
      saved=true;
      await send(`${PREFIX}/${frame.id}/witness-search/save`,values);
      notice("Witness search and marked areas saved.");
      dialog.close();
      await refresh();
    }}));
    return false;
  },{confirm:"Search witnesses"});
}

function boxSketch(frame,{onBox}){
  const image=img(`/api/file/${frame.id}/work`,`Current camera photo ${frame.name}`,"");
  const canvas=h("canvas",{width:frame.extent[0],height:frame.extent[1],"aria-hidden":"true"});
  const scene=h("div",{class:"scene marking"},image,canvas);
  const lead=h("figcaption",null,"Drag a box around one marked area.");
  const ctx=canvas.getContext("2d");
  let start=null;
  const pointAt=(event)=>{
    const r=canvas.getBoundingClientRect();
    return [Math.min(1,Math.max(0,(event.clientX-r.left)/r.width)),
      Math.min(1,Math.max(0,(event.clientY-r.top)/r.height))];
  };
  const draw=(a,b)=>{
    ctx.clearRect(0,0,canvas.width,canvas.height);
    ctx.strokeStyle="#f5c578";ctx.lineWidth=Math.max(2,canvas.width/320);
    ctx.setLineDash([9,6]);
    ctx.strokeRect(a[0]*canvas.width,a[1]*canvas.height,(b[0]-a[0])*canvas.width,(b[1]-a[1])*canvas.height);
    ctx.setLineDash([]);
  };
  canvas.addEventListener("pointerdown",event=>{
    start=pointAt(event);canvas.setPointerCapture(event.pointerId);draw(start,start);
  });
  canvas.addEventListener("pointermove",event=>{if(start)draw(start,pointAt(event));});
  canvas.addEventListener("pointerup",event=>{
    if(!start)return;
    const end=pointAt(event);
    const box=[Math.min(start[0],end[0]),Math.min(start[1],end[1]),
      Math.abs(end[0]-start[0]),Math.abs(end[1]-start[1])].map(v=>Number(v.toFixed(5)));
    start=null;
    if(box[2]<0.01||box[3]<0.01){
      lead.replaceChildren("That box was too small. Drag around the marking again.");
      return;
    }
    lead.replaceChildren(h("strong",null,`Source box [x, y, width, height]: ${box.join(", ")}`));
    onBox(box);
  });
  return h("figure",{class:"viewer",style:{margin:"0"}},scene,lead);
}

export function savedSearchPanel(item,frame,refresh){
  showModal(item.name,[
    h("div",{class:"full stacked"},img(`/api/glances/${item.id}/marked.png`,`Marked areas saved with ${item.name}`),
      h("p",{class:"muted"},`${human(lookups.poses,item.query?.pose)} · ${(item.query?.regions||[]).map(region=>human(lookups.regions,region.region)).join(", ")}`),
      ...resultBlocks(item.report,{onSave:null})),
  ],()=>true,{confirm:"Done"});
}

export function witnessPane(frame,refresh){
  const saved=frame.glances.filter(item=>item.kind==="witness-search");
  return h("section",{class:"card stacked tight"},h("h2",null,"Marking witnesses"),
    h("p",{class:"muted"},"Mark only the parts this photo shows. An area this photo or the witnesses do not show stays unresolved, never a mismatch."),
    h("div",{class:"actions"},
      button("Find retained witnesses",()=>searchDialog(frame,refresh),"primary")),
    saved.length?saved.map(item=>row(item.name,
      `${human(lookups.poses,item.query?.pose)} · ${(item.query?.regions||[]).map(region=>human(lookups.regions,region.region)).join(", ")}`,
      button("Reopen",()=>savedSearchPanel(item,frame,refresh)),
      button("Search again",()=>searchDialog(frame,refresh,{preset:{...item.query,
        minimum:Math.round((item.query?.minimum??0.3)*100)}})))):
      empty("No saved witness search on this photo."));
}
