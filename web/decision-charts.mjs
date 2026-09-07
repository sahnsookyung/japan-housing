const NS='http://www.w3.org/2000/svg';
const colors=['#275d49','#8b641e','#425ba3'];
function node(tag,attrs={},text){const n=document.createElementNS(NS,tag);for(const [key,value] of Object.entries(attrs))n.setAttribute(key,value);if(text!==undefined)n.textContent=text;return n;}
function frame(container,label,xMax,yMax,xLabel,yLabel){
  container.replaceChildren();const svg=node('svg',{viewBox:'0 0 640 310',role:'img','aria-label':label,class:'plot'});container.append(svg);
  const x=v=>65+v/xMax*535, y=v=>260-v/yMax*225;
  for(let i=0;i<=4;i++) {const yy=y(yMax*i/4);svg.append(node('line',{x1:65,y1:yy,x2:600,y2:yy,stroke:'#e3e8e0'}),node('text',{x:55,y:yy+4,'text-anchor':'end','font-size':10,fill:'#66766d'},Math.round(yMax*i/4).toLocaleString()));const xx=x(xMax*i/4);svg.append(node('text',{x:xx,y:279,'text-anchor':'middle','font-size':10,fill:'#66766d'},Math.round(xMax*i/4).toLocaleString()));}
  svg.append(node('text',{x:330,y:302,'text-anchor':'middle','font-size':11},xLabel),node('text',{x:65,y:17,'font-size':11},yLabel));
  return {svg,x,y};
}
export function tradeoffChart(container,data,onRoom){
  const points=data.points.filter(p=>Number.isFinite(p.price)&&Number.isFinite(p.minutes));
  if(!points.length){container.replaceChildren();const p=document.createElement('p');p.className='empty';p.textContent='No rooms yet have complete costs, contract eligibility and routes to every enabled office. Add reviewed routes in room details or fetch routes after configuring the provider.';container.append(p);return;}
  const {svg,x,y}=frame(container,'Effective cost versus observed commute',Math.max(...points.map(p=>p.price))*1.1,Math.max(1,...points.map(p=>p.minutes))*1.1,'Effective expense · ¥ / month','Observed one-way commute · minutes');
  const frontier=points.filter(p=>p.frontier).sort((a,b)=>a.price-b.price);
  if(frontier.length>1)svg.append(node('polyline',{points:frontier.map(p=>`${x(p.price)},${y(p.minutes)}`).join(' '),fill:'none',stroke:'#275d49','stroke-width':2}));
  for(const p of points){const dot=node('circle',{cx:x(p.price),cy:y(p.minutes),r:p.frontier?6:4,fill:p.frontier?'#275d49':'#a7b8ab',tabindex:0,role:'button','aria-label':`${p.title}, ${p.price} yen, ${p.minutes} minutes`});dot.append(node('title',{},`${p.title} · ¥${p.price.toLocaleString()} · ${p.minutes} min`));dot.addEventListener('click',()=>onRoom(p.id));dot.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();onRoom(p.id);}});svg.append(dot);}
  if(data.chart_total>points.length){const p=document.createElement('p');p.textContent=`Showing ${points.length} of ${data.chart_total} points. Narrow your filters to inspect the remainder.`;container.append(p);}
}
export function costChart(container,series){
  const values=series.flatMap(s=>s.values.map(v=>v.total_yen)).filter(Number.isFinite);
  if(!values.length){container.textContent='Recurring charges are incomplete; there is no comparable cost curve yet.';return;}
  const {svg,x,y}=frame(container,'Equal-period expense at monthly anniversaries',12,Math.max(1,...values)*1.12,'Stay · months','Total expense · ¥');
  series.forEach((s,index)=>{
    for(let i=1;i<s.values.length;i++){const a=s.values[i-1],b=s.values[i];if(a.total_yen==null||b.total_yen==null)continue;const solid=a.complete&&b.complete&&a.contract_eligible===true&&b.contract_eligible===true;svg.append(node('line',{x1:x(a.months),y1:y(a.total_yen),x2:x(b.months),y2:y(b.total_yen),stroke:colors[index],'stroke-width':2,'stroke-dasharray':solid?'none':'5 4'}));}
    const label=document.createElement('p');label.style.color=colors[index];label.textContent=s.title;container.append(label);
  });
  const note=document.createElement('small');note.textContent='Dashed segments have incomplete costs or unconfirmed/ineligible contract lengths. They are not firm quotes.';container.append(note);
}
export function addCalendarMonths(value,months){const [year,month,day]=value.split('-').map(Number);const date=new Date(Date.UTC(year,month-1+months,1));const last=new Date(Date.UTC(date.getUTCFullYear(),date.getUTCMonth()+1,0)).getUTCDate();date.setUTCDate(Math.min(day,last));return date.toISOString().slice(0,10);}
