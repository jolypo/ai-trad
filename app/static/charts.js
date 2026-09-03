(function(){
  const NS='http://www.w3.org/2000/svg';
  const LANG=document.documentElement.lang||'ar';
  const locale=LANG==='ar'?'ar-SA-u-nu-latn':'en-US';
  const numberLocale='en-US';
  const fmtMoney=n=>'$ '+Number(n||0).toLocaleString(numberLocale,{minimumFractionDigits:2,maximumFractionDigits:2});
  const fmtDate=t=>new Intl.DateTimeFormat(locale,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',numberingSystem:'latn'}).format(new Date(Number(t)*1000));
  const el=(tag,attrs={})=>{const x=document.createElementNS(NS,tag);Object.entries(attrs).forEach(([k,v])=>x.setAttribute(k,v));return x};
  function clear(svg){while(svg.firstChild)svg.removeChild(svg.firstChild)}
  function dims(svg){const r=svg.getBoundingClientRect();return{w:Math.max(r.width,320),h:Math.max(r.height,240),pad:{l:88,r:18,t:20,b:38}}}
  function grid(svg,w,h,pad,lo,hi){
    const axisBg=el('rect',{x:0,y:0,width:pad.l-5,height:h-pad.b+2,class:'chart-axis-bg'});svg.appendChild(axisBg);
    for(let i=0;i<6;i++){
      const y=pad.t+i*(h-pad.t-pad.b)/5;
      svg.appendChild(el('line',{x1:pad.l,y1:y,x2:w-pad.r,y2:y,class:'chart-gridline'}));
      const v=hi-(hi-lo)*i/5,t=el('text',{x:pad.l-9,y:y+4,'text-anchor':'end',class:'chart-axis','direction':'ltr'});
      t.textContent=fmtMoney(v);svg.appendChild(t);
    }
    svg.appendChild(el('line',{x1:pad.l,y1:pad.t,x2:pad.l,y2:h-pad.b,class:'chart-axis-line'}));
  }
  function timeAxis(svg,series,xFor,h,pad){
    if(!series?.length)return;
    const picks=[0,Math.floor((series.length-1)/2),series.length-1];
    picks.forEach((idx,k)=>{const p=series[idx],t=el('text',{x:xFor(p,idx),y:h-9,'text-anchor':k===0?'start':(k===2?'end':'middle'),class:'chart-time-axis','direction':'ltr'});t.textContent=fmtDate(p.t);svg.appendChild(t)});
  }
  function attachCrosshair(svg,series,xFor,yFor,formatter){
    const g=el('g',{class:'crosshair hidden'}),vl=el('line',{class:'crosshair-line'}),hl=el('line',{class:'crosshair-line'}),dot=el('circle',{r:4,class:'crosshair-dot'}),box=el('g',{class:'chart-tip'}),rect=el('rect',{rx:8,ry:8,width:180,height:52}),txt1=el('text',{x:10,y:20}),txt2=el('text',{x:10,y:41});box.append(rect,txt1,txt2);g.append(vl,hl,dot,box);svg.appendChild(g);
    const show=(clientX,clientY)=>{const r=svg.getBoundingClientRect(),mx=(clientX-r.left)/Math.max(r.width,1)*r.width;let best=0,bd=Infinity;series.forEach((p,i)=>{const d=Math.abs(xFor(p,i)-mx);if(d<bd){bd=d;best=i}});const p=series[best],x=xFor(p,best),y=yFor(p,best),label=formatter(p);vl.setAttribute('x1',x);vl.setAttribute('x2',x);vl.setAttribute('y1',10);vl.setAttribute('y2',r.height-25);hl.setAttribute('x1',88);hl.setAttribute('x2',r.width-10);hl.setAttribute('y1',y);hl.setAttribute('y2',y);dot.setAttribute('cx',x);dot.setAttribute('cy',y);box.setAttribute('transform',`translate(${Math.min(x+10,r.width-190)},${Math.max(8,y-60)})`);txt1.textContent=label[0];txt2.textContent=label[1];g.classList.remove('hidden')};
    svg.addEventListener('mousemove',e=>show(e.clientX,e.clientY));svg.addEventListener('mouseleave',()=>g.classList.add('hidden'));
    svg.addEventListener('pointerdown',e=>{if(e.pointerType!=='mouse')show(e.clientX,e.clientY)});
  }
  function lineChart(svg,points){
    clear(svg);if(!points||points.length<2){svg.parentElement.classList.add('chart-empty');return null}svg.parentElement.classList.remove('chart-empty');const{w,h,pad}=dims(svg);svg.setAttribute('viewBox',`0 0 ${w} ${h}`);
    const vals=points.map(x=>Number(x.v)),lo=Math.min(...vals),hi=Math.max(...vals),margin=Math.max((hi-lo)*.08,.01),lo2=lo-margin,hi2=hi+margin,span=(hi2-lo2)||1;grid(svg,w,h,pad,lo2,hi2);const xFor=(p,i)=>pad.l+i*(w-pad.l-pad.r)/(points.length-1),yFor=p=>pad.t+(hi2-Number(p.v))*(h-pad.t-pad.b)/span;
    timeAxis(svg,points,xFor,h,pad);const d=points.map((p,i)=>(i?'L':'M')+xFor(p,i).toFixed(2)+' '+yFor(p).toFixed(2)).join(' '),change=Number(points.at(-1).v)-Number(points[0].v),tone=change>0?'positive':(change<0?'negative':'neutral');const area=d+` L ${xFor(points.at(-1),points.length-1)} ${h-pad.b} L ${xFor(points[0],0)} ${h-pad.b} Z`;svg.appendChild(el('path',{d:area,class:'chart-area '+tone}));svg.appendChild(el('path',{d,class:'chart-line '+tone}));const base=Number(points[0].v||0);attachCrosshair(svg,points,xFor,yFor,p=>{const val=Number(p.v||0),chg=val-base,pct=base?chg/base*100:0;return[fmtDate(p.t),`${LANG==='ar'?'القيمة':'Equity'} ${fmtMoney(val)} · ${LANG==='ar'?'التغير':'Change'} ${chg>=0?'+':''}${fmtMoney(chg)} (${pct>=0?'+':''}${pct.toFixed(2)}%)`]});return{lo,hi,change};
  }
  function candleChart(svg,bars){
    clear(svg);if(!bars||bars.length<2){svg.parentElement.classList.add('chart-empty');return}svg.parentElement.classList.remove('chart-empty');const{w,h,pad}=dims(svg);svg.setAttribute('viewBox',`0 0 ${w} ${h}`);const lo=Math.min(...bars.map(x=>x.l)),hi=Math.max(...bars.map(x=>x.h)),span=(hi-lo)||1,inner=w-pad.l-pad.r,step=inner/bars.length,body=Math.max(2,Math.min(9,step*.65));grid(svg,w,h,pad,lo,hi);const xFor=(p,i)=>pad.l+(i+.5)*step,y=v=>pad.t+(hi-v)*(h-pad.t-pad.b)/span;timeAxis(svg,bars,xFor,h,pad);bars.forEach((b,i)=>{const x=xFor(b,i),up=b.c>=b.o,cls=up?'candle-up':'candle-down';svg.appendChild(el('line',{x1:x,x2:x,y1:y(b.h),y2:y(b.l),class:cls+' wick'}));svg.appendChild(el('rect',{x:x-body/2,y:Math.min(y(b.o),y(b.c)),width:body,height:Math.max(1,Math.abs(y(b.o)-y(b.c))),class:cls}))});attachCrosshair(svg,bars,xFor,p=>y(p.c),p=>[new Intl.DateTimeFormat(locale,{dateStyle:'short',timeStyle:'short',numberingSystem:'latn'}).format(new Date(p.t)),`O ${p.o.toFixed(2)} · H ${p.h.toFixed(2)} · L ${p.l.toFixed(2)} · C ${p.c.toFixed(2)}`]);
  }
  function labels(root,pct){const trend=root.querySelector('[data-trend-label]');if(!trend)return;trend.textContent=pct>0?(LANG==='ar'?'صاعد ↑':'Up ↑'):(pct<0?(LANG==='ar'?'هابط ↓':'Down ↓'):(LANG==='ar'?'ثابت →':'Flat →'));trend.className='trend-badge '+(pct>0?'up':(pct<0?'down':'flat'))}
  async function loadPortfolio(root,period){const svg=root.querySelector('svg'),status=root.querySelector('[data-chart-status]');if(status)status.textContent=LANG==='ar'?'جارٍ التحميل…':'Loading…';try{const r=await fetch('/api/chart/portfolio?period='+encodeURIComponent(period),{cache:'no-store'}),d=await r.json(),pts=d.points||[];lineChart(svg,pts);const first=pts[0]?.v,last=pts.at(-1)?.v,pct=first?((last/first-1)*100):0;if(status){status.textContent=pts.length?`${fmtMoney(last)}  ${pct>=0?'+':''}${pct.toFixed(2)}%`:(LANG==='ar'?'لا توجد بيانات للرسم':'No chart data');status.className=pct>=0?'pos':'neg'}if(pts.length){const vals=pts.map(x=>Number(x.v)),hi=Math.max(...vals),lo=Math.min(...vals),chg=Number(last)-Number(first),he=root.querySelector('[data-chart-high]'),le=root.querySelector('[data-chart-low]'),ce=root.querySelector('[data-chart-change]');if(he)he.textContent=fmtMoney(hi);if(le)le.textContent=fmtMoney(lo);if(ce){ce.textContent=`${chg>=0?'+':''}${fmtMoney(chg)} (${pct>=0?'+':''}${pct.toFixed(2)}%)`;ce.className=chg>=0?'pos':'neg'}labels(root,pct)}}catch(e){if(status)status.textContent=LANG==='ar'?'الرسم غير متاح':'Chart unavailable'}}
  async function loadStock(root,symbol,period){const svg=root.querySelector('svg'),status=root.querySelector('[data-chart-status]');if(status)status.textContent=LANG==='ar'?'جارٍ التحميل…':'Loading…';try{const r=await fetch(`/api/chart/stock/${encodeURIComponent(symbol)}?period=${encodeURIComponent(period)}`,{cache:'no-store'}),d=await r.json(),b=d.bars||[];candleChart(svg,b);const first=b[0]?.c,last=b.at(-1)?.c,pct=first?((last/first-1)*100):0;if(status){status.textContent=b.length?`${fmtMoney(last)}  ${pct>=0?'+':''}${pct.toFixed(2)}%`:(LANG==='ar'?'لا توجد بيانات للرسم':'No chart data');status.className=pct>=0?'pos':'neg'}if(b.length)labels(root,pct)}catch(e){if(status)status.textContent=LANG==='ar'?'الرسم غير متاح':'Chart unavailable'}}
  function wire(root,type,symbol){if(!root)return;const buttons=root.querySelectorAll('[data-range]');let active=(root.querySelector('[data-range].active')||buttons[0])?.dataset.range;const run=p=>type==='portfolio'?loadPortfolio(root,p):loadStock(root,symbol,p);buttons.forEach(b=>b.onclick=()=>{buttons.forEach(x=>x.classList.remove('active'));b.classList.add('active');active=b.dataset.range;run(active)});if(active)run(active);setInterval(()=>{if(active&&!document.hidden)run(active)},type==='stock'?10000:10000);window.addEventListener('resize',()=>{if(active)run(active)},{passive:true})}
  window.LTCharts={wirePortfolio:r=>wire(r,'portfolio'),wireStock:(r,s)=>wire(r,'stock',s)};
})();
