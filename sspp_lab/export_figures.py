"""停止采集后按真实数据导出PNG与多页PDF；使用独立进程避免阻塞采集控制。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime,timezone

def export_figures(root,key):
    root=Path(root).resolve()
    if not re.fullmatch(r'(runs|phase_runs)/[A-Za-z0-9_-]+',key):raise ValueError('实验编号无效')
    run=root/'Data'/key
    run.resolve().relative_to((root/'Data').resolve())
    if run.is_symlink() or not run.is_dir():raise ValueError('实验目录无效')
    cache=tempfile.TemporaryDirectory(prefix='sspp-plot-cache-')
    os.environ.setdefault('MPLCONFIGDIR',cache.name)
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import pyplot as plt,font_manager
    from matplotlib.backends.backend_pdf import PdfPages
    import numpy as np
    from .diagnostics import build_diagnostics
    font=Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')
    if font.exists():
        font_manager.fontManager.addfont(str(font));plt.rcParams['font.family']=font_manager.FontProperties(fname=str(font)).get_name()
    plt.rcParams['axes.unicode_minus']=False
    model=build_diagnostics(run,'phase' if key.startswith('phase_runs/') else 'sweep')
    plots=[(group,p) for group,items in model['groups'].items() for p in items]
    if not plots:raise ValueError('没有可绘制的真实数据')
    outputs=[];temporary=run/'.diagnostic_report.tmp.pdf'
    with PdfPages(temporary) as pdf:
        for index,(group,p) in enumerate(plots,1):
            fig,ax=plt.subplots(figsize=(11,6.7),layout='constrained')
            try:
                if p['kind']=='heatmap':
                    z=np.array([[np.nan if v is None else v for v in row] for row in p['z']])
                    img=ax.pcolormesh(p['x'],p['y'],z,shading='nearest',cmap='viridis')
                    fig.colorbar(img,ax=ax,label=p.get('colorbar','数值'))
                elif p['kind']=='segments':
                    names=list(dict.fromkeys(s['label'] for s in p['segments']))
                    for segment in p['segments']:
                        y=names.index(segment['label']);color='#b5bac0' if segment['transition'] else segment['color']
                        ax.broken_barh([(segment['start'],max(.01,segment['end']-segment['start']))],(y-.35,.7),facecolors=color)
                    ax.set_yticks(range(len(names)),names)
                else:
                    for s in p['series']:
                        if not s['data']:continue
                        xy=np.asarray(s['data']);x,y=xy[:,0],xy[:,1]
                        error=np.array([np.nan if v is None else v for v in s.get('errors',[])])
                        if p['kind']=='scatter':ax.scatter(x,y,s=8,alpha=.45,label=s['label'],color=s['color'])
                        elif p['kind']=='bar':
                            width=.7 if p.get('xticks') else ((x[-1]-x[0])/max(1,len(x))*0.9 if len(x)>1 else .6)
                            ax.bar(x,y,width=width,color=s['color'],alpha=.8,label=s['label'])
                            if len(error)==len(y):ax.errorbar(x,y,yerr=error,fmt='none',ecolor='#263a4b',capsize=4)
                        else:
                            ax.plot(x,y,lw=1.1,label=s['label'],color=s['color'])
                            if len(error)==len(y):ax.errorbar(x,y,yerr=error,fmt='none',ecolor=s['color'],capsize=4)
                    for line in p.get('horizontal',[]):ax.axhline(line['y'],ls='--',lw=.8,color='#a65959',label=line['label'])
                    for line in p.get('vertical',[]):ax.axvline(line['x'],ls='--',lw=.8,color='#a65959',label=line['label'])
                    if p.get('xticks'):ax.set_xticks(range(len(p['xticks'])),p['xticks'],rotation=25,ha='right')
                    if p.get('baseline_interval'):ax.axvspan(*p['baseline_interval'],color='#b7d3cb',alpha=.2,label='静止参考')
                    for segment in p.get('segments',[]):
                        if segment['transition']:ax.axvspan(segment['start'],segment['end'],color='#aaa',alpha=.13)
                    if p.get('equal_aspect'):ax.set_aspect('equal',adjustable='datalim')
                    handles,labels=ax.get_legend_handles_labels()
                    if handles:ax.legend(loc='best',fontsize=8,ncols=2)
                ax.set_title(p['title'],fontsize=14,loc='left');ax.set_xlabel(p['xlabel']);ax.set_ylabel(p['ylabel']);ax.grid(alpha=.18)
                subtitle=f'{key} | 状态：{model["status"]} | '+p['note']
                import textwrap
                fig.suptitle('\n'.join(textwrap.wrap(subtitle,100)),fontsize=8,color='#566576')
                filename=f'diagnostic_{index:02d}_{p["id"]}.png'
                tmp=run/('.'+filename)
                fig.savefig(tmp,format='png',dpi=170);tmp.replace(run/filename);pdf.savefig(fig)
                outputs.append({'file':filename,'id':p['id'],'title':p['title'],'group':group,'note':p['note']})
            finally:plt.close(fig)
    temporary.replace(run/'diagnostic_report.pdf')
    record={'schema_version':1,'created_utc':datetime.now(timezone.utc).isoformat(),'run':key,
            'data_status':model['status'],'plots':outputs,'pdf':'diagnostic_report.pdf',
            'source_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),Path(__file__).with_name('diagnostics.py')]},
            'input_manifest_sha256':hashlib.sha256((run/'manifest.json').read_bytes()).hexdigest(),
            'limits':'真实RX数据；TX图仅数学设定。数字幅值/PSD未标定RF功率、S21或毫米位移。'}
    temp=run/'.diagnostic_figures.tmp.json';temp.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n');temp.replace(run/'diagnostic_figures.json')
    cache.cleanup()
    return {'key':key,'figures':outputs,'report':'diagnostic_report.pdf','count':len(outputs)}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--key',required=True)
    args=parser.parse_args();print(json.dumps(export_figures(args.root,args.key),ensure_ascii=False))
