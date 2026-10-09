'use strict';
const $=id=>document.getElementById(id);
const token=document.querySelector('meta[name="sspp-token"]').content;
let defaults={},snapshot={},viewing=null,curve={mode:'sweep',points:[]},pollBusy=false,lastStatus='',markCounters={};
const labels={starting:'初始化设备',running:'记录中',stopping:'正在停止',saving:'正在保存',complete:'实验完成',stopped:'已停止',failed:'实验未完成',stop_unconfirmed:'停止状态待确认',initialization_failed:'设备初始化失败'};
const busyStates=new Set(['starting','running','stopping','saving']);
async function api(path,body){
  const response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-SSPP-Token':token},body:JSON.stringify(body)});
  const data=await response.json();if(!response.ok)throw new Error(data.error||'请求失败');return data;
}
function notice(message,error=false){$('notice').textContent=message;$('notice').className=error?'error':'';$('notice').hidden=!message;}
function formConfig(){
  const data={...defaults};for(const [name] of new FormData($('experimentForm'))){const field=$('experimentForm').elements.namedItem(name);data[name]=field.type==='number'?Number(field.value):field.value;}
  // disabled fieldset 的 FormData 为空，启动只会发生于未锁定的表单。
  data.setup_confirmed=$('setupConfirmed').checked;return data;
}
function showMode(mode=$('mode').value){
  document.querySelectorAll('.mode-sweep').forEach(x=>x.hidden=mode!=='sweep');
  document.querySelectorAll('.mode-phase').forEach(x=>x.hidden=mode!=='phase');
  $('marksPanel').hidden=mode!=='phase';
  if(!viewing)setChartMode(mode);
}
function setChartMode(mode){
  $('chartTitle').textContent=(viewing?'已保存':'实时')+(mode==='phase'?'幅相曲线':'扫频曲线');
  $('phaseChart').hidden=mode!=='phase';
  $('amplitudeCaption').textContent=mode==='phase'?'相对初始参考的幅度变化（dB） · 时间（s）':'FFT 峰值幅度（dBFS 估计值） · 载频（GHz）';
  $('chartHint').textContent=mode==='phase'?'相位使用本次静止参考，未自动扣除漂移。阶段由你手动标记，距离未标定。':'扫频各轮使用不同颜色。数字幅度未经绝对功率或 S21 标定。';
}
function renderChart(canvasId,series){
  const canvas=$(canvasId),rect=canvas.getBoundingClientRect(),ratio=window.devicePixelRatio||1;
  if(rect.width<1||rect.height<1)return;
  canvas.width=Math.round(rect.width*ratio);canvas.height=Math.round(rect.height*ratio);
  const ctx=canvas.getContext('2d');ctx.scale(ratio,ratio);
  const w=rect.width,h=rect.height,pad={l:55,r:15,t:20,b:28};
  const points=series.flatMap(s=>s.data).filter(p=>Number.isFinite(p.x)&&Number.isFinite(p.y));
  let x0=0,x1=1,y0=-1,y1=1;
  if(points.length){x0=Math.min(...points.map(p=>p.x));x1=Math.max(...points.map(p=>p.x));y0=Math.min(...points.map(p=>p.y));y1=Math.max(...points.map(p=>p.y));const extra=Math.max((y1-y0)*.13,.05);y0-=extra;y1+=extra;if(x0===x1){x0-=.02;x1+=.02;}}
  const X=x=>pad.l+(x-x0)/(x1-x0)*(w-pad.l-pad.r),Y=y=>h-pad.b-(y-y0)/(y1-y0)*(h-pad.t-pad.b);
  ctx.font='10px -apple-system,sans-serif';ctx.lineWidth=1;
  for(let i=0;i<=4;i++){const y=y0+(y1-y0)*i/4,py=Y(y);ctx.strokeStyle='#e8eef2';ctx.beginPath();ctx.moveTo(pad.l,py);ctx.lineTo(w-pad.r,py);ctx.stroke();ctx.fillStyle='#788996';ctx.textAlign='right';ctx.fillText(y.toFixed(2),pad.l-9,py+3);}
  for(let i=0;i<=4;i++){const x=x0+(x1-x0)*i/4;ctx.fillStyle='#788996';ctx.textAlign='center';ctx.fillText(x.toFixed(curve.mode==='phase'?1:3),X(x),h-8);}
  for(const seriesItem of series){ctx.strokeStyle=seriesItem.color;ctx.lineWidth=1.8;ctx.beginPath();let first=true;for(const p of seriesItem.data){if(!Number.isFinite(p.y))continue;if(first){ctx.moveTo(X(p.x),Y(p.y));first=false;}else ctx.lineTo(X(p.x),Y(p.y));}ctx.stroke();if(curve.mode==='sweep'){ctx.fillStyle=seriesItem.color;for(const p of seriesItem.data){ctx.beginPath();ctx.arc(X(p.x),Y(p.y),2.6,0,2*Math.PI);ctx.fill();}}}
}
function draw(){
  const colors=['#087f85','#e0973e','#6977b6','#b76683','#4c9271'];
  if(curve.mode==='phase'){
    renderChart('amplitudeCanvas',[{color:colors[0],data:curve.points.map(p=>({x:p.x,y:p.amplitude}))}]);
    renderChart('phaseCanvas',[{color:colors[1],data:curve.points.map(p=>({x:p.x,y:p.phase}))}]);
  }else{
    const groups=new Map();for(const p of curve.points){if(!groups.has(p.repeat))groups.set(p.repeat,[]);groups.get(p.repeat).push({x:p.carrier_hz/1e9,y:p.amplitude_dB});}
    renderChart('amplitudeCanvas',[...groups].map(([key,data])=>({color:colors[(key-1)%colors.length],data})));
  }
  $('emptyChart').hidden=curve.points.length>0;
  if(typeof refreshDiagnostics==='function')refreshDiagnostics();
}
function updateState(data){
  snapshot=data;const job=data.job,busy=job&&busyStates.has(job.status),mode=job?.config.mode||$('mode').value;
  $('configFields').disabled=!!busy;$('lockedBadge').hidden=!busy;
  $('startButton').disabled=!!busy||!$('profile').options.length;
  $('planButton').disabled=!!busy||!$('profile').options.length;
  $('stopButton').disabled=!busy||['stopping','saving'].includes(job.status);
  $('statusText').textContent=job?(labels[job.status]||job.status):'待开始';
  let detail='服务启动不会自动收发';
  if(job){if(job.rf_stopped)detail='收发与工作线程停止已确认';else if(job.status==='running')detail=mode==='phase'?'保持静止建立参考；基线通过后可标记阶段':'正在逐点测量并保存原始 IQ';else if(job.status==='starting')detail='正在初始化所选设备';else if(job.status==='saving')detail='收发收尾后导出实验结果';else if(job.status==='stopping')detail='等待停止命令和线程收尾';else if(job.status==='stop_unconfirmed')detail='请检查设备和运行日志';}
  $('statusDetail').textContent=detail;
  $('markFields').disabled=!(busy&&job.baseline&&job.status==='running');
  $('baselineState').textContent=job?.baseline?'静止基线通过':'等待静止基线';
  if(!viewing){
    setChartMode(mode);$('marksPanel').hidden=mode!=='phase';
    $('metricLabel1').textContent=mode==='phase'?'幅度变化 dB':'已测频点';
    $('metricLabel2').textContent=mode==='phase'?'相位变化 °':'当前轮次';
    if(mode==='phase'){$('metric1').textContent=job?.live?.amplitude_dB?.toFixed(3)??'—';$('metric2').textContent=job?.live?.phase_deg?.toFixed(2)??'—';$('metric3').textContent=job?.live?.label||job?.config.label||'待开始';}
    else{$('metric1').textContent=data.points?.length?`${data.points.length} / ${job.config.points*job.config.repeat}`:'—';$('metric2').textContent=data.points?.at(-1)?.repeat??'—';$('metric3').textContent=job?.config.label||'待开始';}
  }
  $('logs').textContent=data.logs?.join('\n')||'等待实验开始';
  $('eventList').replaceChildren();for(const e of data.events||[]){if(e.event!=='WEB_MARK_REQUEST'&&e.event!=='CONDITION_READY')continue;const item=document.createElement('li');item.textContent=e.event==='WEB_MARK_REQUEST'?`${e.label} · 已发送标记${e.contact_reported?' · 报告接触':''}`:`${e.label} · 阶段开始（${e.t_s?.toFixed(2)} s）`;$('eventList').append(item);}
  const newStatus=job?.status||'';if(newStatus!==lastStatus){lastStatus=newStatus;if(job&&['complete','stopped','failed','stop_unconfirmed','initialization_failed'].includes(newStatus)){refreshHistory();if(job.errors?.length)notice(job.errors.join('；'),true);else notice(job.rf_stopped?'实验记录已保存，收发停止已确认。':'请核查停止状态。',!job.rf_stopped);}}
}
async function poll(){
  if(pollBusy)return;pollBusy=true;
  try{const state=await api('/api/status');updateState(state);$('connectionText').textContent='本机服务已连接';document.querySelector('.connection').classList.add('online');if(!viewing){curve=await api('/api/curve');if(!curve.mode)curve={mode:$('mode').value,points:[]};draw();}else if(typeof refreshDiagnostics==='function')refreshDiagnostics();}
  catch(e){$('connectionText').textContent='服务连接中断';document.querySelector('.connection').classList.remove('online');if(snapshot.job&&busyStates.has(snapshot.job.status))notice('服务连接中断，当前收发状态未知。请检查服务终端。',true);}
  finally{pollBusy=false;}
}
function timeLabel(utc){return utc?new Date(utc).toLocaleString('zh-CN',{hour12:false}):'时间未知';}
async function refreshHistory(){
  try{const items=await api('/api/history');$('historyList').replaceChildren();if(!items.length){const p=document.createElement('p');p.className='hint';p.textContent='尚无实验记录';$('historyList').append(p);}
    for(const item of items){const row=document.createElement('div');row.className='history-row';const left=document.createElement('div'),button=document.createElement('button');button.type='button';button.textContent=item.label;button.addEventListener('click',()=>viewRun(item));const small=document.createElement('small');small.textContent=`${timeLabel(item.started_utc)} · ${{sweep:'扫频',phase:'幅相',teacher:'老师历史界面'}[item.mode]}${item.sensor?' · '+item.sensor:''}`;left.append(button,small);const pill=document.createElement('span');pill.className='pill '+(item.status==='complete'?'complete':'failed');pill.textContent=labels[item.status]||(item.status==='failed_or_incomplete'?'未完成':item.status);row.append(left,pill);$('historyList').append(row);}}
  catch(e){notice(e.message,true);}
}
async function viewRun(item){
  try{const detail=await api('/api/run?key='+encodeURIComponent(item.key));viewing=item.key;resetDiagnosticView();curve=detail.curve;$('liveButton').hidden=false;setChartMode(curve.mode||item.mode);$('runDetails').hidden=false;$('runDetailTitle').textContent=item.label;$('runDetailSummary').textContent=`${item.key} · ${timeLabel(detail.metadata.started_utc)} · ${detail.metadata.status||''}`;
    updateRunDownloads(detail);
    $('metricLabel1').textContent='保存记录';$('metric1').textContent=curve.stored_windows||curve.points.length;$('metricLabel2').textContent='方式';$('metric2').textContent=item.mode==='phase'?'连续幅相':'扫频';$('metric3').textContent=item.label;draw();
    showFigureGallery(detail);}
  catch(e){notice(e.message,true);}
}
function updateRunDownloads(detail){$('fileList').replaceChildren();for(const file of detail.files){const a=document.createElement('a');a.href='/api/file?key='+encodeURIComponent(detail.key)+'&name='+encodeURIComponent(file);a.textContent=file;a.target='_blank';a.rel='noopener';$('fileList').append(a);}}
async function sendMark(label){
  try{await api('/api/mark',{label,settle:Number($('settleSeconds').value),contact:$('contactReported').checked});notice('标记已发送：'+label);await poll();}
  catch(e){notice(e.message,true);}
}
$('experimentForm').addEventListener('submit',async e=>{e.preventDefault();$('startButton').disabled=true;try{const data=await api('/api/start',formConfig());viewing=null;resetDiagnosticView();$('liveButton').hidden=true;$('runDetails').hidden=true;markCounters={};curve={mode:data.job.config.mode,points:[]};notice('实验已启动，请保持初始状态。');updateState(data);await poll();}catch(err){notice(err.message,true);$('startButton').disabled=false;}});
$('planButton').addEventListener('click',async()=>{try{const data=await api('/api/plan',formConfig());notice(data.valid?'参数与设备配置检查通过。尚未打开硬件。':data.summary);}catch(e){notice(e.message,true);}});
$('stopButton').addEventListener('click',async()=>{try{updateState(await api('/api/stop',{}));notice('已请求停止，正在等待收发结束及数据保存。');}catch(e){notice(e.message,true);}});
$('mode').addEventListener('change',()=>{showMode();if(!snapshot.job){curve={mode:$('mode').value,points:[]};draw();}});
$('condition').addEventListener('change',()=>{$('label').value={empty:'empty_baseline',finger:'finger_far_initial',material:'material_initial'}[$('condition').value];$('setupConfirmed').checked=false;});
$('profile').addEventListener('change',()=>{$('setupConfirmed').checked=false;});
document.querySelectorAll('[data-mark]').forEach(button=>button.addEventListener('click',()=>{const base=button.dataset.mark;markCounters[base]=(markCounters[base]||0)+1;sendMark(base+'_'+markCounters[base]);}));
$('markButton').addEventListener('click',()=>sendMark($('markLabel').value.trim()));
$('refreshHistory').addEventListener('click',refreshHistory);
$('liveButton').addEventListener('click',()=>{viewing=null;resetDiagnosticView();$('liveButton').hidden=true;$('runDetails').hidden=true;poll();});
window.addEventListener('resize',draw);
async function init(){try{const data=await api('/api/bootstrap');defaults=data.defaults;for(const p of data.profiles){const option=document.createElement('option');option.value=p.id;option.textContent=p.name;$('profile').append(option);}if(!data.profiles.length)notice('未找到设备配置，请检查本机配置文件。',true);showMode();await refreshHistory();await poll();setInterval(poll,1000);}catch(e){notice(e.message,true);}}
init();
