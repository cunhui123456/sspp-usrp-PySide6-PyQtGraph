'use strict';
let detailedModel={groups:{},segments:[]},activePlotGroup='diagnostic',diagnosticBusy=false,exportBusy=false,lastDiagnosticFetch=0,diagnosticKey='',expandedPlot=null;
const groupNames={diagnostic:'接收诊断',sweep:'扫频分析',phase:'幅相分析',quality:'采集质量'};
function plotNumber(v){if(!Number.isFinite(v))return '—';if(Math.abs(v)>0&&Math.abs(v)<.001)return v.toExponential(2);return Number(v.toPrecision(5)).toString();}
function boundsOfPlot(p){
  let points=p.series.flatMap(s=>s.data).filter(v=>v.every(Number.isFinite));
  if(p.kind==='heatmap')points=p.x.flatMap(x=>p.y.map(y=>[x,y]));
  if(p.kind==='segments')points=p.segments.flatMap(s=>[[s.start,0],[s.end,1]]);
  let x0=points.length?Math.min(...points.map(v=>v[0])):0,x1=points.length?Math.max(...points.map(v=>v[0])):1;
  let y0=points.length?Math.min(...points.map(v=>v[1])):-1,y1=points.length?Math.max(...points.map(v=>v[1])):1;
  for(const s of p.series)if(s.errors)s.data.forEach(([x,y],i)=>{if(Number.isFinite(s.errors[i])){y0=Math.min(y0,y-s.errors[i]);y1=Math.max(y1,y+s.errors[i]);}});
  for(const l of p.horizontal||[]){y0=Math.min(y0,l.y);y1=Math.max(y1,l.y);}
  if(p.kind==='bar'){y0=Math.min(0,y0);y1=Math.max(0,y1);const n=p.series[0]?.data.length||1,edge=n>1?(x1-x0)/(n-1)/2:.5;x0-=edge;x1+=edge;}
  if(x0===x1){x0-=.5;x1+=.5;}if(y0===y1){const d=Math.max(Math.abs(y0)*.05,.05);y0-=d;y1+=d;}
  if(p.kind!=='heatmap'&&p.kind!=='segments'){const d=(y1-y0)*.1;y0-=d;y1+=d;}
  if(p.equal_aspect){const d=Math.max(x1-x0,y1-y0);const mx=(x0+x1)/2,my=(y0+y1)/2;x0=mx-d/2;x1=mx+d/2;y0=my-d/2;y1=my+d/2;}
  return {x0,x1,y0,y1};
}
function heatColor(v,lo,hi){const t=Math.max(0,Math.min(1,(v-lo)/(hi-lo||1)));const stops=[[46,34,84],[48,102,141],[38,157,134],[115,194,90],[239,218,75]];const i=Math.min(3,Math.floor(t*4)),f=t*4-i;return `rgb(${stops[i].map((a,k)=>Math.round(a*(1-f)+stops[i+1][k]*f)).join(',')})`;}
function renderDetailedCanvas(canvas,p,tip){
  const rect=canvas.getBoundingClientRect();if(rect.width<1||rect.height<1)return;
  const ratio=window.devicePixelRatio||1;canvas.width=Math.round(rect.width*ratio);canvas.height=Math.round(rect.height*ratio);
  const ctx=canvas.getContext('2d');ctx.scale(ratio,ratio);const w=rect.width,h=rect.height;
  const pad={l:58,r:p.kind==='heatmap'?54:18,t:16,b:p.xticks?70:48};let {x0,x1,y0,y1}=boundsOfPlot(p);
  if(p.equal_aspect){const units=Math.max((x1-x0)/(w-pad.l-pad.r),(y1-y0)/(h-pad.t-pad.b)),mx=(x0+x1)/2,my=(y0+y1)/2;x0=mx-units*(w-pad.l-pad.r)/2;x1=mx+units*(w-pad.l-pad.r)/2;y0=my-units*(h-pad.t-pad.b)/2;y1=my+units*(h-pad.t-pad.b)/2;}
  const X=x=>pad.l+(x-x0)/(x1-x0)*(w-pad.l-pad.r),Y=y=>h-pad.b-(y-y0)/(y1-y0)*(h-pad.t-pad.b);
  ctx.fillStyle='#fff';ctx.fillRect(0,0,w,h);ctx.font='10px -apple-system,sans-serif';
  if(p.baseline_interval){ctx.fillStyle='#e5f0eb';ctx.fillRect(X(Math.max(x0,p.baseline_interval[0])),pad.t,Math.max(0,X(Math.min(x1,p.baseline_interval[1]))-X(Math.max(x0,p.baseline_interval[0]))),h-pad.t-pad.b);}
  for(const s of p.segments||[])if(s.transition&&s.end>=x0&&s.start<=x1){ctx.fillStyle='#edf0f3';ctx.fillRect(X(Math.max(x0,s.start)),pad.t,X(Math.min(x1,s.end))-X(Math.max(x0,s.start)),h-pad.t-pad.b);}
  for(let i=0;i<=4;i++){const y=y0+(y1-y0)*i/4;ctx.strokeStyle='#e7edf0';ctx.beginPath();ctx.moveTo(pad.l,Y(y));ctx.lineTo(w-pad.r,Y(y));ctx.stroke();ctx.fillStyle='#617785';ctx.textAlign='right';if(p.kind!=='segments')ctx.fillText(plotNumber(y),pad.l-8,Y(y)+3);}
  if(p.kind==='heatmap'){
    const values=p.z.flat().filter(Number.isFinite),lo=values.length?Math.min(...values):0,hi=values.length?Math.max(...values):1;
    const dx=(w-pad.l-pad.r)/Math.max(1,p.x.length),dy=(h-pad.t-pad.b)/Math.max(1,p.y.length);
    for(let row=0;row<p.y.length;row++)for(let col=0;col<p.x.length;col++){const v=p.z[row]?.[col];ctx.fillStyle=Number.isFinite(v)?heatColor(v,lo,hi):'#edf0f3';ctx.fillRect(pad.l+col*dx,h-pad.b-(row+1)*dy,dx+.5,dy+.5);}
    for(let i=0;i<100;i++){ctx.fillStyle=heatColor(lo+(hi-lo)*i/99,lo,hi);ctx.fillRect(w-pad.r+12,h-pad.b-(h-pad.t-pad.b)*(i+1)/100,10,(h-pad.t-pad.b)/100+.5);}
    ctx.fillStyle='#617785';ctx.textAlign='left';ctx.fillText(plotNumber(hi),w-pad.r+25,pad.t+7);ctx.fillText(plotNumber(lo),w-pad.r+25,h-pad.b);
  }else if(p.kind==='segments'){
    const n=p.segments.length,rowH=(h-pad.t-pad.b)/Math.max(1,n);
    for(let i=0;i<n;i++){const s=p.segments[i],top=pad.t+i*rowH;ctx.fillStyle=s.transition?'#b7bec5':s.color;ctx.fillRect(X(s.start),top+2,Math.max(3,X(s.end)-X(s.start)),Math.max(3,rowH-4));}
  }else{
    for(const s of p.series){ctx.strokeStyle=s.color;ctx.fillStyle=s.color;ctx.lineWidth=1.4;ctx.beginPath();let started=false;
      for(let i=0;i<s.data.length;i++){const [x,y]=s.data[i];if(!Number.isFinite(x)||!Number.isFinite(y)){started=false;continue;}
        if(p.kind==='scatter'){ctx.globalAlpha=.45;ctx.fillRect(X(x)-1.5,Y(y)-1.5,3,3);ctx.globalAlpha=1;}
        else if(p.kind==='bar'){const bw=p.xticks?Math.min(40,(w-pad.l-pad.r)/Math.max(1,s.data.length+1)*.65):Math.max(2,(w-pad.l-pad.r)/Math.max(1,s.data.length)*.8);ctx.globalAlpha=.75;ctx.fillRect(X(x)-bw/2,Math.min(Y(0),Y(y)),bw,Math.max(1,Math.abs(Y(0)-Y(y))));ctx.globalAlpha=1;}
        else{if(!started){ctx.moveTo(X(x),Y(y));started=true;}else ctx.lineTo(X(x),Y(y));}
      }if(p.kind==='line')ctx.stroke();
      if(s.errors)for(let i=0;i<s.data.length;i++){const [x,y]=s.data[i],e=s.errors[i];if(!Number.isFinite(e))continue;ctx.beginPath();ctx.moveTo(X(x),Y(y-e));ctx.lineTo(X(x),Y(y+e));ctx.moveTo(X(x)-4,Y(y-e));ctx.lineTo(X(x)+4,Y(y-e));ctx.moveTo(X(x)-4,Y(y+e));ctx.lineTo(X(x)+4,Y(y+e));ctx.stroke();}
    }
    ctx.setLineDash([4,4]);ctx.strokeStyle='#b85f65';for(const l of p.horizontal||[]){ctx.beginPath();ctx.moveTo(pad.l,Y(l.y));ctx.lineTo(w-pad.r,Y(l.y));ctx.stroke();}for(const l of p.vertical||[]){ctx.beginPath();ctx.moveTo(X(l.x),pad.t);ctx.lineTo(X(l.x),h-pad.b);ctx.stroke();}ctx.setLineDash([]);
  }
  ctx.fillStyle='#617785';ctx.textAlign='center';
  if(p.xticks){p.xticks.forEach((label,i)=>{ctx.save();ctx.translate(X(i),h-pad.b+8);ctx.rotate(-.35);ctx.textAlign='right';ctx.fillText(label.length>22?label.slice(0,20)+'…':label,0,10);ctx.restore();});}
  else for(let i=0;i<=4;i++){const x=x0+(x1-x0)*i/4;ctx.fillText(plotNumber(x),X(x),h-pad.b+17);}
  ctx.fillText(p.xlabel,w/2,h-4);ctx.save();ctx.translate(10,h/2);ctx.rotate(-Math.PI/2);ctx.fillText(p.ylabel,0,0);ctx.restore();
  if(tip){canvas.onpointermove=e=>{const px=e.clientX-canvas.getBoundingClientRect().left,py=e.clientY-canvas.getBoundingClientRect().top,x=x0+(px-pad.l)/(w-pad.l-pad.r)*(x1-x0);let message='';
      if(p.kind==='heatmap'){const col=Math.max(0,Math.min(p.x.length-1,Math.floor((px-pad.l)/(w-pad.l-pad.r)*p.x.length))),row=Math.max(0,Math.min(p.y.length-1,Math.floor((h-pad.b-py)/(h-pad.t-pad.b)*p.y.length)));message=`${p.xlabel}: ${plotNumber(p.x[col])} · ${p.ylabel}: ${plotNumber(p.y[row])} · ${p.colorbar}: ${plotNumber(p.z[row]?.[col])}`;}
      else if(p.kind==='segments'){const s=p.segments[Math.max(0,Math.min(p.segments.length-1,Math.floor((py-pad.t)/(h-pad.t-pad.b)*p.segments.length)))];if(s)message=`${s.label} · ${s.start.toFixed(3)}–${s.end.toFixed(3)} s${s.contact===true?' · 报告接触':s.contact===false?' · 报告未接触':''}`;}
      else{const vals=p.series.map(s=>{let point=s.data.reduce((best,v)=>Math.abs(v[0]-x)<Math.abs(best[0]-x)?v:best,[Infinity,0]);return Number.isFinite(point[0])?`${s.label}: (${plotNumber(point[0])}, ${plotNumber(point[1])})`:'';});message=vals.filter(Boolean).join(' · ');}tip.textContent=message;};canvas.onpointerleave=()=>{tip.textContent='移动指针查看读数；放大后可查看完整坐标和图例。';};}
}
function downloadCanvas(canvas,name){canvas.toBlob(blob=>{if(!blob)return;const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name+'.png';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);});}
function plotLegend(target,p){target.replaceChildren();const items=p.kind==='segments'?p.segments.map(s=>({color:s.transition?'#b7bec5':s.color,label:`${s.label} · ${s.start.toFixed(1)}–${s.end.toFixed(1)} s${s.contact===true?' · 报告接触':s.contact===false?' · 报告未接触':''}`})):p.series;for(const s of items){const span=document.createElement('span');const dot=document.createElement('i');dot.style.background=s.color;span.append(dot,document.createTextNode(s.label));target.append(span);}if(p.kind==='heatmap')target.textContent=p.colorbar;for(const l of [...(p.horizontal||[]),...(p.vertical||[])]){const span=document.createElement('span');span.textContent=l.label;target.append(span);}}
function drawDetailedPlots(){
  const grid=$('plotGrid');grid.replaceChildren();const plots=detailedModel.groups?.[activePlotGroup]||[];
  if(!plots.length){const p=document.createElement('p');p.className='hint';p.textContent=activePlotGroup==='sweep'&&detailedModel.mode==='phase'?'本记录为连续幅相，请选择幅相分析。':activePlotGroup==='phase'&&detailedModel.mode==='sweep'?'本记录为扫频，请选择扫频分析。':detailedModel.sample?.reason||'暂无对应真实数据；选择历史实验或开始记录后显示。';grid.append(p);return;}
  for(const plot of plots){const card=document.createElement('article');card.className='plot-card';const heading=document.createElement('div');heading.className='plot-card-heading';const title=document.createElement('h4');title.textContent=plot.title;const expand=document.createElement('button');expand.type='button';expand.className='text-button';expand.textContent='放大';expand.setAttribute('aria-label','放大 '+plot.title);expand.onclick=()=>openPlot(plot);heading.append(title,expand);
    const canvas=document.createElement('canvas');canvas.className='detail-canvas';canvas.setAttribute('aria-label',plot.title);canvas.dataset.plotId=plot.id;const legend=document.createElement('div');legend.className='plot-legend';plotLegend(legend,plot);const note=document.createElement('p');note.className='hint';note.textContent=plot.note;const tip=document.createElement('p');tip.className='plot-readout';tip.textContent='移动指针查看读数；放大后可查看完整坐标和图例。';card.append(heading,canvas,legend,note,tip);grid.append(card);renderDetailedCanvas(canvas,plot,tip);}
  if(expandedPlot)renderDetailedCanvas($('expandedCanvas'),expandedPlot,$('expandedReadout'));
}
function openPlot(plot){expandedPlot=plot;$('expandedTitle').textContent=plot.title;$('expandedNote').textContent=plot.note;plotLegend($('expandedLegend'),plot);$('plotDialog').showModal();renderDetailedCanvas($('expandedCanvas'),plot,$('expandedReadout'));}
function resetDiagnosticView(){detailedModel={groups:{},sample:{reason:'正在读取所选记录的真实数据…'}};$('plotStatus').textContent='正在读取真实实验数据…';$('rxPreviewSummary').textContent='等待读取IQ来源';$('qualityFacts').replaceChildren();$('figureGallery').replaceChildren();$('figureGalleryCount').textContent='';$('exportProgress').replaceChildren();$('exportPlots').disabled=true;drawDetailedPlots();}
async function refreshDiagnostics(force=false){
  const key=viewing||'',now=Date.now();if(diagnosticBusy||(!force&&key===diagnosticKey&&now-lastDiagnosticFetch<3000))return;diagnosticBusy=true;
  try{const model=await api('/api/diagnostics'+(key?'?key='+encodeURIComponent(key):''));if((viewing||'')!==key)return;const unchanged=JSON.stringify(model)===JSON.stringify(detailedModel);detailedModel=model;diagnosticKey=key;lastDiagnosticFetch=now;
    $('plotStatus').textContent=model.key?`${model.key} · ${model.chart_count}张分析图 · ${labels[model.status]||model.status}`:'选择有真实数据的历史记录，或开始实验';
    $('rxPreviewSummary').textContent=model.sample?.available?`RX预览：载频 ${plotNumber(model.sample.carrier_hz/1e9)} GHz · 单音 ${plotNumber(model.sample.tone_hz/1000)} kHz · ${model.sample.source}`:model.sample?.reason||'尚无RX IQ';
    $('qualityFacts').replaceChildren();for(const fact of model.facts||[]){const box=document.createElement('div'),name=document.createElement('span'),value=document.createElement('strong');name.textContent=fact.name;value.textContent=fact.value;box.append(name,value);$('qualityFacts').append(box);}
    $('plotTabSweep').hidden=model.mode==='phase';$('plotTabPhase').hidden=model.mode==='sweep';if((model.mode==='phase'&&activePlotGroup==='sweep')||(model.mode==='sweep'&&activePlotGroup==='phase'))activePlotGroup=model.mode;
    document.querySelectorAll('[data-plot-group]').forEach(b=>{b.classList.toggle('active',b.dataset.plotGroup===activePlotGroup);b.setAttribute('aria-pressed',b.dataset.plotGroup===activePlotGroup);});
    $('exportPlots').disabled=exportBusy||!model.chart_count||!!(snapshot.job&&busyStates.has(snapshot.job.status));if(!unchanged)drawDetailedPlots();
  }catch(e){$('plotStatus').textContent='图形读取失败：'+e.message;}finally{diagnosticBusy=false;}
}
function showFigureGallery(detail,clearProgress=true){
  const gallery=$('figureGallery');gallery.replaceChildren();const key=detail.key;
  if(clearProgress)$('exportProgress').replaceChildren();
  const titles=new Map((detail.figures||[]).map(f=>[f.file,f.title]));
  for(const name of detail.files.filter(n=>n.endsWith('.png'))){const a=document.createElement('a');a.href='/api/file?key='+encodeURIComponent(key)+'&name='+encodeURIComponent(name);a.target='_blank';a.rel='noopener';const img=document.createElement('img');img.loading='lazy';img.src=a.href;img.alt=titles.get(name)||name;const label=document.createElement('span');label.textContent=titles.get(name)||name;a.append(img,label);gallery.append(a);}
  $('figureGalleryCount').textContent=gallery.childElementCount+'张PNG';
  if(clearProgress&&detail.files.includes('diagnostic_report.pdf'))appendReportLink(key);
}
function appendReportLink(key){const a=document.createElement('a');a.href='/api/file?key='+encodeURIComponent(key)+'&name=diagnostic_report.pdf';a.target='_blank';a.rel='noopener';a.textContent='打开／保存PDF图册';$('exportProgress').append(a);}
document.querySelectorAll('[data-plot-group]').forEach(b=>b.addEventListener('click',()=>{activePlotGroup=b.dataset.plotGroup;document.querySelectorAll('[data-plot-group]').forEach(x=>{x.classList.toggle('active',x===b);x.setAttribute('aria-pressed',x===b);});drawDetailedPlots();}));
$('refreshPlots').onclick=()=>refreshDiagnostics(true);
$('closePlot').onclick=()=>{$('plotDialog').close();expandedPlot=null;};$('plotDialog').addEventListener('close',()=>{expandedPlot=null;});
$('savePlotView').onclick=()=>{if(expandedPlot)downloadCanvas($('expandedCanvas'),expandedPlot.id+'_view');};
$('exportPlots').onclick=async()=>{const button=$('exportPlots');exportBusy=true;button.disabled=true;const key=viewing||detailedModel.key||null;$('exportProgress').textContent='正在从真实数据生成PNG和PDF图册…';try{const result=await api('/api/export',{key});if((viewing||detailedModel.key)!==result.key){notice(`${result.key} 的图册已生成：${result.count} 张PNG。`);return;}const detail=await api('/api/run?key='+encodeURIComponent(result.key));showFigureGallery(detail,false);if(viewing===result.key)updateRunDownloads(detail);$('figureGalleryDetails').open=true;$('exportProgress').replaceChildren(document.createTextNode(`已生成 ${result.count} 张PNG和多页PDF。 `));appendReportLink(result.key);}catch(e){$('exportProgress').textContent='生成失败：'+e.message;}finally{exportBusy=false;button.disabled=!!(snapshot.job&&busyStates.has(snapshot.job.status));}};
window.addEventListener('resize',()=>{drawDetailedPlots();if(expandedPlot)renderDetailedCanvas($('expandedCanvas'),expandedPlot,$('expandedReadout'));});
