export const q=(selector,root=document)=>root.querySelector(selector);
export const nodes=(selector,root=document)=>[...root.querySelectorAll(selector)];
export const workspace=q("#workspace");
const modal=q("#dialog");
let noticeTimer;
export let lookups={};
export function setLookups(data){lookups=data;}
export function h(kind,props,...children){
  const item=document.createElement(kind);
  for(const [key,value] of Object.entries(props||{})){
    if(value===false||value==null)continue;
    if(key==="class")item.className=value;
    else if(key==="text")item.textContent=value;
    else if(key==="style")Object.assign(item.style,value);
    else if(key.startsWith("on"))item.addEventListener(key.slice(2),value);
    else if(key in item&&!key.startsWith("aria-"))item[key]=value;
    else item.setAttribute(key,value===true?"":String(value));
  }
  for(const child of children.flat(Infinity))
    if(child!==null&&child!==undefined&&child!==false)item.append(child instanceof Node?child:document.createTextNode(String(child)));
  return item;
}
export function button(title,action,style=""){
  const node=h("button",{type:"button",class:style},title);
  node.addEventListener("click",async ()=>{
    node.disabled=true;
    try{await action(node);}
    catch(error){notice(error.message);}
    finally{node.disabled=false;}
  });
  return node;
}
export function link(title,target,style){return h("a",{href:target,class:style||""},title);}
export async function api(route,options={}){
  const payload={...options};
  if(Object.hasOwn(payload,"json")){
    payload.headers={...payload.headers,"Content-Type":"application/json"};
    payload.body=JSON.stringify(payload.json);
    delete payload.json;
  }
  const response=await fetch(route,payload);
  if(!response.ok){
    let message=`Request failed (${response.status}).`;
    try{message=(await response.json()).error.message||message;}catch{}
    throw new Error(message);
  }
  return (response.headers.get("content-type")||"").includes("application/json")?response.json():response.blob();
}
export const send=(route,json,method="POST")=>api(route,{method,json});
export const file=(key,asset)=>`/api/file/${encodeURIComponent(key)}/${encodeURIComponent(asset)}`;
export const pct=value=>Number.isFinite(value)?`${Math.round(value*100)}%`:"—";
export const num=(value,d=2)=>Number.isFinite(value)?Number(value.toFixed(d)).toLocaleString():"—";
export function human(dict,key){return dict?.[key]||key||"—";}
export function stamp(frame){return frame.local_stamp?.wall_time?.replace("T"," ")||frame.stamp_utc?.replace("T"," ")||"Time unresolved";}
export const list=(array)=>array.length?array.join(", "):"—";
export const badge=(name,type="")=>h("span",{class:`badge ${type}`},name);
export const empty=(text)=>h("p",{class:"empty"},text);
export function notice(text){
  clearTimeout(noticeTimer);
  q("#notice").textContent=text||"";
  if(text)noticeTimer=setTimeout(()=>{q("#notice").textContent="";},4000);
}
export function title(head,description,...actions){
  return h("header",{class:"topline"},h("div",null,h("h1",null,head),description&&h("p",null,description)),
    actions.length&&h("div",{class:"actions"},actions));
}
export function pane(head,...content){
  return h("section",{class:"card stacked tight"},head?h("h2",null,head):null,content);
}
export function metric(label,val){
  return h("div",{class:"stat"},h("small",null,label),h("strong",null,val));
}
export function definition(values){
  return h("dl",null,values.flatMap(([term,val])=>[h("dt",null,term),h("dd",null,val??"—")]));
}
export function row(head,sub,...actions){
  return h("div",{class:"item-row"},h("div",{class:"grow"},h("strong",null,head),sub&&h("span",{class:"muted"},sub)),actions);
}
export function table(headings,items){
  if(!items.length)return empty("No matching rows.");
  return h("div",{class:"table-box"},h("table",null,
    h("thead",null,h("tr",null,headings.map(label=>h("th",{scope:"col"},label)))),
    h("tbody",null,items.map(values=>h("tr",null,values.map(piece=>h("td",null,piece??"—")))))));
}
export function field(label,name,value="",attributes={}){
  const {type="text",wide=false,help=null,...input}=attributes;
  const control=type==="area"?h("textarea",{name,value,...input}):h("input",{name,value,type,...input});
  const entry=h("label",{class:wide?"full":""},label,control);
  if(help)entry.append(h("span",{class:"help"},help));
  return entry;
}
export function pick(label,name,options,value="",prompt=null){
  const select=h("select",{name});
  const pairs=Array.isArray(options)?options:Object.entries(options||{});
  if(prompt!==null)select.append(h("option",{value:""},prompt));
  for(const [id,title] of pairs)select.append(h("option",{value:id},title));
  if(value!==""||prompt!==null)select.value=value;
  return h("label",null,label,select);
}
export const choices=(label,name,values,selected=[])=>h("fieldset",{class:"full"},h("legend",null,label),
  h("div",{class:"choices"},(Array.isArray(values)?values:Object.entries(values||{})).map(([key,label])=>
    h("label",{class:"check"},h("input",{type:"checkbox",name,value:key,checked:selected.includes(key)}),label))));
export const check=(title,name,checked=false)=>h("label",{class:"check"},h("input",{type:"checkbox",name,checked}),title);
export const data=(form)=>Object.fromEntries(new FormData(form));
export function jsonField(values,name,def){
  if(!values[name]&&def!==undefined)return def;
  try{return JSON.parse(values[name]);}
  catch{throw new Error(`Enter a valid JSON value for ${name.replaceAll("_"," ")}.`);}
}
export function showModal(title,fields,commit,options={}){
  if(modal.open)modal.close();
  const warning=h("p",{class:"error",hidden:true,role:"alert"});
  const form=h("form",null);
  const confirm=h("button",{type:"submit",class:"primary"},options.confirm||"Save");
  const close=()=>{if(modal.firstElementChild===form)modal.close();};
  const error=message=>{warning.textContent=message||"";warning.hidden=!message;};
  const formHead=h("div",{class:"dialog-head"},h("h2",{id:"dialog-title"},title),button("Close",close));
  const layout=h("div",{class:"form-grid"},fields);
  form.append(formHead,h("div",{class:"dialog-body"},layout,warning),
    h("div",{class:"dialog-actions"},button("Cancel",close),confirm));
  form.addEventListener("submit",async event=>{
    event.preventDefault();
    confirm.disabled=true;
    error("");
    try{const keep=await commit(form,data(form));if(keep!==false)close();}
    catch(exc){error(exc.message);}
    finally{confirm.disabled=false;}
  });
  modal.replaceChildren(form);
  modal.showModal();
  return {form,layout,error,close};
}
export function detail(title,payload){
  return h("details",null,h("summary",null,title),h("pre",null,JSON.stringify(payload,null,2)));
}
export function img(source,alt,style="report-image"){return h("img",{src:source,alt,class:style,loading:"lazy"});}
export function frameCards(frames,selection,callback){
  return h("div",{class:"grid frames"},frames.map(f=>h("section",{class:"card frame-card"},
    h("button",{class:"open-frame",onclick:()=>callback(f.id),type:"button"},
      img(file(f.id,"thumbnail"),`Camera photo ${f.name}`,"thumbnail"),
      h("strong",null,f.name)),
    h("div",{class:"title-row"},h("small",null,stamp(f)),badge(human(lookups.review,f.review),f.review)),
    h("div",{class:"title-row"},h("small",null,human(lookups.lighting,f.illumination)),
      f.origin==="synthetic"&&badge("Synthetic","synthetic")),
    selection&&h("label",{class:"check"},h("input",{type:"checkbox",checked:selection.has(f.id),onchange:event=>
      event.target.checked?selection.add(f.id):selection.delete(f.id)}),"Use this frame"))));
}
export function route(section,key=null){location.hash=key?`${section}/${key}`:section;}
export function recordLink(label,section,key){return link(label,`#${section}/${key}`);}
