'use strict';
/* ── State ─────────────────────────────────────────────── */
const S={tStartMs:null,tEndMs:null,curMs:null,playing:false,timer:null,
  speedMult:60,tickMs:50,lastWall:0,
  bounds:null,circle:null,mask:null,circleCenter:null,radiusM:500,
  trajs:{},cacheStart:null,cacheEnd:null,prefetching:false,
  sampleSec:2,   // kept in sync with whatever fetchTrajectory last used
  accidents:[],accMarkers:[],accidentVins:{},accMode:'persist',
  charts:{},eventLabels:{},collisionLabels:{},
  markers:new Map(),statusCounts:{normal:0,accel:0,brake:0,turn:0,collision:0},
  // Collision-list / focus-mode state
  severityFilter:'all',typeFilter:'all',panelCollapsed:false,
  focusedVin:null,focusCenterMs:null,focusWaypoints:null,focusPolyline:null,
  focusMarkers:[],
  // Per-day risk (updates as playback crosses day boundaries)
  dailyCrashes:null,dailyEvents:null,lastRiskDay:null,
  // Road-Focus mode (Kamphaeng Phet 6 Rd partitioned into sections A/B/C)
  mode:'full',road:null,  // mode: 'full' | 'road' | 'heatmap'
  mapStyle:'dark',
  roadRects:[],roadLabels:[],roadMask:null,
  activeSection:null,
  sectionAnalytics:{},                                  // {A:{...},B:{...},C:{...}}
  sectionDaily:{A:null,B:null,C:null},                  // per-section daily series
  lastSectionRiskDay:{A:null,B:null,C:null},
  // Heatmap mode state
  heatLayer:null,heatZonePoly:null,
  heatEventType:0,heatSpeedBracket:0,heatHour:24,
  staticMode:false};

// ±N minutes of single-vehicle track shown during investigation
const FOCUS_WINDOW_MIN = 3;

/* ── Circle geometry helper ────────────────────────────── */
// Returns N lat/lng pairs tracing a circle of radiusM metres around [lat,lng].
function circleLatLngs(lat,lng,radiusM){
  const n=72,pts=[];
  for(let i=0;i<=n;i++){
    const a=(i/n)*2*Math.PI;
    const dlat=radiusM*Math.cos(a)/111000;
    const dlng=radiusM*Math.sin(a)/(111000*Math.cos(lat*Math.PI/180));
    pts.push([lat+dlat,lng+dlng]);
  }
  return pts;
}

/* ── Map ───────────────────────────────────────────────── */
const map=L.map('map',{zoomControl:true,preferCanvas:true});
let baseTileLayer=L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{
  attribution:'&copy; OSM &copy; CARTO',subdomains:'abcd',maxZoom:19}).addTo(map);

/* ── Helpers ───────────────────────────────────────────── */
const iso=ms=>new Date(ms).toISOString().slice(0,19);
const fmtDate=ms=>ms?new Date(ms).toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'numeric'}).toUpperCase():'—';
const fmtStamp=ms=>{if(!ms)return'—';const d=new Date(ms);return fmtDate(ms)+' | '+d.toTimeString().slice(0,8)};
const dirLabel=d=>{if(d==null||isNaN(d))return'—';const D=["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"];return D[Math.round(d/22.5)%16]};
const evtLabel=e=>S.eventLabels[e]||('Event '+e);
const colLabel=c=>S.collisionLabels[c]||('Type '+c);
const sevColor=s=>s==='high'?'#dc2626':s==='medium'?'#f97316':'#f59e0b';
const riskLevel=s=>s<=3?{l:'LOW',c:'risk-low'}:s<=6?{l:'MEDIUM',c:'risk-medium'}:{l:'HIGH',c:'risk-high'};

function arrowSvg(dir,color,size){
  size=size||16;
  return `<div style="width:${size}px;height:${size}px;transform:rotate(${dir||0}deg);transition:transform .3s">
    <svg viewBox="0 0 20 20" width="${size}" height="${size}">
      <polygon points="10,2 17,17 10,13 3,17" fill="${color}" stroke="rgba(0,0,0,.3)" stroke-width="1"/>
    </svg></div>`;
}

function vehicleColor(vin,evt,prevSpd,curSpd){
  if(vin in S.accidentVins)return'#dc2626';
  // Use firmware event_type first — it's the ground-truth signal.
  // DB stores integers 1/2/3; JSON may deserialise as number or string.
  const e=evt==null?null:Number(evt);
  if(e===1)return'#f59e0b';   // Harsh Braking  → amber
  if(e===2)return'#0d9488';   // Sudden Accel   → teal
  if(e===3)return'#8b5cf6';   // Sharp Turn     → purple
  // Fallback for Basic (0x11) rows: detect from speed delta
  if(prevSpd!=null&&curSpd!=null){
    const d=curSpd-prevSpd;
    if(d>5)return'#0d9488';
    if(d<-5)return'#f59e0b';
  }
  return'#3b82f6';
}

/* ── API ───────────────────────────────────────────────── */
async function api(url){const r=await fetch(url);if(!r.ok)throw new Error(await r.text());return r.json()}

function circleBbox(){
  const c=S.circleCenter,r=S.radiusM;
  const dLa=r/111000,dLo=r/(111000*Math.cos(c.lat*Math.PI/180));
  return{lat_min:c.lat-dLa,lat_max:c.lat+dLa,lon_min:c.lng-dLo,lon_max:c.lng+dLo};
}

// Union bbox over all road sections — used when fetching trajectory / accidents
// / analytics in Road-Focus mode.  If a single section is "active" (clicked),
// returns just that section's bbox so analytics scope tightens.
function roadBbox(){
  if(!S.road||!S.road.sections.length)return circleBbox();
  let secs=S.road.sections;
  if(S.activeSection)secs=secs.filter(s=>s.id===S.activeSection);
  let la_lo=Infinity,la_hi=-Infinity,lo_lo=Infinity,lo_hi=-Infinity;
  secs.forEach(s=>{
    if(s.lat_min<la_lo)la_lo=s.lat_min;
    if(s.lat_max>la_hi)la_hi=s.lat_max;
    if(s.lon_min<lo_lo)lo_lo=s.lon_min;
    if(s.lon_max>lo_hi)lo_hi=s.lon_max;
  });
  return{lat_min:la_lo,lat_max:la_hi,lon_min:lo_lo,lon_max:lo_hi};
}

// Active bbox respects the current view mode.  Every fetch goes through this
// so toggling Full ↔ Road just works.
function activeBbox(){
  return S.mode==='road'?roadBbox():circleBbox();
}

// Standard ray-cast point-in-polygon for a closed ring [[lat,lon],...].
function pointInPolygon(lat,lon,poly){
  if(!poly||poly.length<3)return false;
  let inside=false;
  for(let i=0,j=poly.length-1;i<poly.length;j=i++){
    const yi=poly[i][0],xi=poly[i][1],yj=poly[j][0],xj=poly[j][1];
    const intersect=((yi>lat)!==(yj>lat))&&(lon<(xj-xi)*(lat-yi)/(yj-yi)+xi);
    if(intersect)inside=!inside;
  }
  return inside;
}

// Which section a point falls into.  Uses the oriented-rectangle polygon if
// available (accurate to the road's true bearing), falls back to bbox if not.
function sectionFor(lat,lon){
  if(!S.road)return null;
  for(const s of S.road.sections){
    if(s.polygon&&s.polygon.length){
      if(pointInPolygon(lat,lon,s.polygon))return s.id;
    }else if(lat>=s.lat_min&&lat<=s.lat_max&&lon>=s.lon_min&&lon<=s.lon_max){
      return s.id;
    }
  }
  return null;
}

async function fetchTrajectory(tStart,tEnd){
  const bb=activeBbox();
  // For slow speeds (≤30s/s) request every row (sample_sec=1).
  // At higher speeds a coarser sample keeps the wire payload manageable.
  const samp=S.speedMult<=30?1:S.speedMult<=60?2:S.speedMult<=300?5:10;
  S.sampleSec=samp; // keep state in sync so interp can reference it
  const p=new URLSearchParams({t_start:iso(tStart),t_end:iso(tEnd),
    lat_min:bb.lat_min,lat_max:bb.lat_max,lon_min:bb.lon_min,lon_max:bb.lon_max,sample_sec:samp});
  return api('/api/trajectory?'+p);
}

async function fetchAccidents(){
  const bb=activeBbox();
  const p=new URLSearchParams({lat_min:bb.lat_min,lat_max:bb.lat_max,lon_min:bb.lon_min,lon_max:bb.lon_max,
    t_start:iso(S.tStartMs),t_end:iso(S.tEndMs)});
  if(S.mode==='road'){
    p.set('section_id',S.activeSection||'all');
  }
  const d=await api('/api/accidents?'+p);
  S.accidents=d.accidents||[];S.accidentVins=d.accident_vins||{};
  updateTypeFilterOptions();
  renderCollisionList();
}

async function fetchAnalytics(){
  const bb=activeBbox();
  const p=new URLSearchParams({lat_min:bb.lat_min,lat_max:bb.lat_max,lon_min:bb.lon_min,lon_max:bb.lon_max,
    t_start:iso(S.tStartMs),t_end:iso(S.tEndMs)});
  const cm=document.getElementById('cm-date').value;
  if(cm)p.set('countermeasure_date',cm);
  if(S.mode==='road'){
    p.set('section_id',S.activeSection||'all');
  }
  return api('/api/analytics?'+p);
}

// Per-section analytics: parallel /api/analytics calls, one per section.
// Each tile re-renders the moment its own fetch resolves so the user sees
// progress instead of staring at three placeholder dashes for 30 seconds.
async function fetchSectionAnalytics(){
  if(!S.road)return;
  const cm=document.getElementById('cm-date').value;
  // Mark every section as loading up-front
  S.road.sections.forEach(s=>{S.sectionAnalytics[s.id]={_loading:true}});
  renderSectionTiles();

  await Promise.allSettled(S.road.sections.map(async s=>{
    const p=new URLSearchParams({lat_min:s.lat_min,lat_max:s.lat_max,
      lon_min:s.lon_min,lon_max:s.lon_max,
      t_start:iso(S.tStartMs),t_end:iso(S.tEndMs)});
    if(cm)p.set('countermeasure_date',cm);
    p.set('section_id',s.id);
    const t0=performance.now();
    try{
      const d=await api('/api/analytics?'+p);
      S.sectionAnalytics[s.id]=d;
      const dc={},de={};
      (d.crash_frequency||[]).forEach(x=>{dc[x.date]=x.count});
      (d.daily_events   ||[]).forEach(x=>{de[x.date]=x.count});
      S.sectionDaily[s.id]={crashes:dc,events:de};
      S.lastSectionRiskDay[s.id]=null;
      console.log(`[analytics] section ${s.id} ✓ ${Math.round(performance.now()-t0)}ms`);
    }catch(e){
      console.error(`[analytics] section ${s.id} ✕`,e);
      S.sectionAnalytics[s.id]={_error:e.message||'Request failed'};
    }
    renderSectionTiles();
    updateSectionRiskForCurrentDay();
  }));
}

function getDist(lat1, lon1, lat2, lon2) {
  const R = 6371e3;
  const p1 = lat1 * Math.PI/180, p2 = lat2 * Math.PI/180;
  const dp = (lat2-lat1) * Math.PI/180, dl = (lon2-lon1) * Math.PI/180;
  const a = Math.sin(dp/2) * Math.sin(dp/2) + Math.cos(p1) * Math.cos(p2) * Math.sin(dl/2) * Math.sin(dl/2);
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
}

function interp(wps,tMs){
  if(!wps||!wps.length)return null;
  const t0 = new Date(wps[0].t).getTime();
  if(tMs < t0) return null; // FIX: Don't show car before its first waypoint

  let i=0;
  while(i<wps.length-1&&new Date(wps[i+1].t).getTime()<=tMs)i++;
  const a=wps[i],tA=new Date(a.t).getTime();
  
  if(i>=wps.length-1){
    const el=(tMs-tA)/1000;
    // Dead-reckon past the last known waypoint before hiding the marker.
    // 77% of records have NULL vehicle_speed, so we CANNOT use a.s to decide
    // whether the car is "moving" — null coerces to 0 and would cut the timeout
    // to 10 s (shorter than the original 15 s), causing cars to flash off in
    // under 0.2 real seconds at 60x playback.
    // Fix: always use sampleSec × 20 (min 60 s) so cars glide smoothly to the
    // window edge regardless of whether speed was recorded.
    const disappearAfter = Math.max(60, S.sampleSec * 20);
    if(el>disappearAfter||el<0)return null;
    const sm=(a.s||0)/3.6,dr=a.d*Math.PI/180;
    return{lat:a.la+sm*el*Math.cos(dr)/111000,
      lon:a.lo+sm*el*Math.sin(dr)/(111000*Math.cos(a.la*Math.PI/180)),
      dir:a.d,spd:a.s,evt:a.e,col:a.c,prevSpd:a.s};
  }
  
  const b=wps[i+1],tB=new Date(b.t).getTime(),gap=tB-tA,frac=Math.max(0,Math.min(1,(tMs-tA)/gap));
  
  // Calculate speed if missing
  let spdA = a.s, spdB = b.s;
  if(spdA == null || spdB == null){
    const dM = getDist(a.la, a.lo, b.la, b.lo);
    const calcSpd = (dM / (gap/1000)) * 3.6;
    if(spdA == null) spdA = calcSpd;
    if(spdB == null) spdB = calcSpd;
  }

  if(gap>10000){
    const el=(tMs-tA)/1000,sm=(spdA||0)/3.6,dr=a.d*Math.PI/180;
    let dDir=b.d-a.d;if(dDir>180)dDir-=360;if(dDir<-180)dDir+=360;
    return{lat:a.la+sm*el*Math.cos(dr)/111000,
      lon:a.lo+sm*el*Math.sin(dr)/(111000*Math.cos(a.la*Math.PI/180)),
      dir:a.d+dDir*frac,spd:spdA,evt:a.e,col:a.c,prevSpd:spdA};
  }
  
  let dDir=b.d-a.d;if(dDir>180)dDir-=360;if(dDir<-180)dDir+=360;
  return{lat:a.la+(b.la-a.la)*frac,lon:a.lo+(b.lo-a.lo)*frac,
    dir:a.d+dDir*frac,spd:spdA+(spdB-spdA)*frac,
    evt:a.e,col:a.c,prevSpd:spdA};
}

/* ── Render frame ──────────────────────────────────────── */
function renderFrame(){
  if(!S.curMs)return;
  if(S.staticMode){
    S.markers.forEach((m,vin)=>{m.remove()});
    S.markers.clear();
    S.statusCounts={normal:0,accel:0,brake:0,turn:0,collision:0};
    updateStatusUI();
    clearAccMarkers();
    updateRiskForCurrentDay();
    if(S.mode==='road')updateSectionRiskForCurrentDay();
    return;
  }
  const counts={normal:0,accel:0,brake:0,turn:0,collision:0};
  const seen=new Set();
  for(const[vin,wps]of Object.entries(S.trajs)){
    const p=interp(wps,S.curMs);
    if(!p){if(S.markers.has(vin)){S.markers.get(vin).remove();S.markers.delete(vin)}continue;}
    // Hide vehicles outside the active geometry (circle in Full mode, road
    // sections in Road-Focus mode).  In Road-Focus, also respect any pinned
    // active section so click-to-focus visibly culls vehicles in real time.
    if(S.mode==='road'){
      const sec=sectionFor(p.lat,p.lon);
      const inRoad=sec!==null;
      const inActive=!S.activeSection||sec===S.activeSection;
      if(!inRoad||!inActive){
        if(S.markers.has(vin)){S.markers.get(vin).remove();S.markers.delete(vin)}
        continue;
      }
    }else if(S.circleCenter&&getDist(p.lat,p.lon,S.circleCenter.lat,S.circleCenter.lng)>S.radiusM){
      if(S.markers.has(vin)){S.markers.get(vin).remove();S.markers.delete(vin)}
      continue;
    }
    seen.add(vin);
    const col=vehicleColor(vin,p.evt,p.prevSpd,p.spd);
    const isCol=vin in S.accidentVins&&S.curMs>=new Date(S.accidentVins[vin]).getTime();
    if(isCol)counts.collision++;
    else if(col==='#0d9488')counts.accel++;
    else if(col==='#f59e0b')counts.brake++;
    else if(col==='#8b5cf6')counts.turn++;
    else counts.normal++;

    const icon=L.divIcon({className:'',html:arrowSvg(p.dir,col),iconSize:[16,16],iconAnchor:[8,8]});
    const spdStr = p.spd != null && !isNaN(p.spd) ? Math.round(p.spd) + ' km/h' : '—';
    const dirStr = p.dir != null && !isNaN(p.dir) ? `${Math.round(p.dir)}° ${dirLabel(p.dir)}` : '—';
    const popup=`<b>${vin.slice(-8)}</b><br>${spdStr} | ${dirStr}`
      +(p.evt?`<br><span style="color:#f59e0b">${evtLabel(p.evt)}</span>`:'')
      +(p.col?`<br><b style="color:#dc2626">${colLabel(p.col)}</b>`:'');
    if(S.markers.has(vin)){
      const m=S.markers.get(vin);m.setLatLng([p.lat,p.lon]);m.setIcon(icon);m.getPopup()?.setContent(popup);
    }else{
      const m=L.marker([p.lat,p.lon],{icon,zIndexOffset:100}).bindPopup(popup).addTo(map);
      S.markers.set(vin,m);
    }
  }
  S.markers.forEach((m,vin)=>{if(!seen.has(vin)){m.remove();S.markers.delete(vin)}});
  S.statusCounts=counts;
  updateStatusUI();
  renderAccidents();
  applyFocusStyling();
  updateRiskForCurrentDay();
  if(S.mode==='road')updateSectionRiskForCurrentDay();
}

/* ── Accident rendering ────────────────────────────────── */
// In flash mode, show accidents only within this data-time window (ms).
// 5 minutes of data time — enough to see an event flash past at any speed.
const ACC_FLASH_WINDOW_MS = 5 * 60 * 1000;

function clearAccMarkers(){
  S.accMarkers.forEach(m=>{if(m&&map.hasLayer(m))m.remove()});
  S.accMarkers=[];
}

function _makeAccIcon(sc){
  return L.divIcon({className:'',
    html:`<div style="width:28px;height:28px;display:flex;align-items:center;justify-content:center;
      background:rgba(220,38,38,.18);border:2px solid ${sc};border-radius:50%;
      box-shadow:0 0 10px 3px ${sc}88,0 0 0 4px rgba(220,38,38,.08)">
      <svg width="13" height="13" viewBox="0 0 10 10"><polygon points="5,0.5 9.5,9.5 0.5,9.5" fill="${sc}" stroke="#fff" stroke-width=".5"/></svg></div>`,
    iconSize:[28,28],iconAnchor:[14,14]});
}

function renderAccidents(){
  if(S.staticMode || S.accMode==='hidden'){clearAccMarkers();return;}
  const now=S.curMs;
  S.accidents.forEach((acc,i)=>{
    const accMs=new Date(acc.timestamp).getTime();
    // Determine whether this accident should be visible right now
    const inPast        = accMs<=now;
    const inFlashWindow = inPast&&(now-accMs)<=ACC_FLASH_WINDOW_MS;
    const timeOk        = S.accMode==='persist'?inPast:inFlashWindow;
    const sevOk         = S.severityFilter==='all'||acc.severity===S.severityFilter;
    const typeOk        = S.typeFilter==='all'||String(acc.collision_type)===String(S.typeFilter);
    // In Road-Focus mode, hide collisions that fall OUTSIDE the road sections
    // (and outside the pinned section if one is active).
    let roadOk = true;
    if(S.mode==='road'){
      const sec=sectionFor(acc.lat,acc.lon);
      roadOk = sec!==null && (!S.activeSection||sec===S.activeSection);
    }
    const shouldShow    = timeOk && sevOk && typeOk && roadOk;

    if(!shouldShow){
      // Remove marker if it exists (handles time reversal and flash expiry)
      if(S.accMarkers[i]){
        if(map.hasLayer(S.accMarkers[i]))S.accMarkers[i].remove();
        delete S.accMarkers[i];
      }
      return;
    }
    // Already on the map — nothing to do
    if(S.accMarkers[i]&&map.hasLayer(S.accMarkers[i]))return;
    // Create (or re-create after reversal)
    const sc=sevColor(acc.severity);
    const popup=`<b>Collision</b><br>${acc.timestamp.replace('T',' ').slice(0,19)}<br>
      ${acc.collision_label}<br>Severity: <b>${acc.severity.toUpperCase()}</b><br>
      Speed: ${acc.speed!=null?acc.speed+' km/h':'—'}<br>G: ${acc.gx}`;
    S.accMarkers[i]=L.marker([acc.lat,acc.lon],{icon:_makeAccIcon(sc),zIndexOffset:2000})
      .bindPopup(popup).addTo(map);
  });
}

/* ── Status UI ─────────────────────────────────────────── */
function updateStatusUI(){
  const c=S.statusCounts,tot=c.normal+c.accel+c.brake+c.turn+c.collision;
  document.getElementById('sp-total').textContent=tot;
  document.getElementById('sp-normal').textContent=c.normal;
  document.getElementById('sp-accel').textContent=c.accel;
  document.getElementById('sp-brake').textContent=c.brake;
  document.getElementById('sp-turn').textContent=c.turn;
  document.getElementById('sp-collision').textContent=c.collision;
}

/* ── Charts ────────────────────────────────────────────── */
const CO={responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}}};

function initCharts(){
  S.charts.baEvents=new Chart(document.getElementById('chart-ba-events'),{type:'bar',
    data:{labels:['Before','After'],datasets:[{data:[0,0],backgroundColor:['#2563eb','#0d9488'],borderRadius:3,barPercentage:.5}]},
    options:{...CO,scales:{x:{ticks:{font:{size:9},color:'#64748b'},grid:{display:false}},y:{ticks:{font:{size:9},color:'#64748b',maxTicksLimit:4},grid:{color:'#f1f5f9'}}}}});
  S.charts.baRisk=new Chart(document.getElementById('chart-ba-risk'),{type:'bar',
    data:{labels:['Before','After'],datasets:[{data:[0,0],backgroundColor:['#2563eb','#0d9488'],borderRadius:3,barPercentage:.5}]},
    options:{...CO,scales:{x:{ticks:{font:{size:9},color:'#64748b'},grid:{display:false}},y:{ticks:{font:{size:9},color:'#64748b',maxTicksLimit:4},grid:{color:'#f1f5f9'}}}}});
  S.charts.freq=new Chart(document.getElementById('chart-freq'),{type:'line',
    data:{labels:[],datasets:[{data:[],borderColor:'#2563eb',backgroundColor:'rgba(37,99,235,.08)',fill:true,tension:.35,pointRadius:1.5,borderWidth:1.5}]},
    options:{...CO,scales:{x:{ticks:{font:{size:8},color:'#64748b',maxRotation:0,maxTicksLimit:5},grid:{display:false}},y:{ticks:{font:{size:9},color:'#64748b',maxTicksLimit:4},grid:{color:'#f1f5f9'}}}}});
  S.charts.events=new Chart(document.getElementById('chart-events'),{type:'doughnut',
    data:{labels:['Harsh Brake','Sudden Accel','Sharp Turn','Collision'],
      datasets:[{data:[1,1,1,1],backgroundColor:['#3b82f6','#0d9488','#8b5cf6','#dc2626'],borderWidth:0,hoverOffset:4}]},
    options:{responsive:true,maintainAspectRatio:false,cutout:'58%',plugins:{legend:{position:'right',labels:{font:{size:9},color:'#374151',padding:6,usePointStyle:true,pointStyleWidth:7}}}}});
}

function drawGauge(score){
  const cv=document.getElementById('gauge'),W=cv.offsetWidth||260,H=cv.offsetHeight||70;
  const dpr=window.devicePixelRatio||1;cv.width=W*dpr;cv.height=H*dpr;
  const ctx=cv.getContext('2d');ctx.scale(dpr,dpr);
  const cx=W/2,cy=H+2,r=Math.min(W*.42,H*1.1);
  ctx.beginPath();ctx.arc(cx,cy,r,Math.PI,0,false);ctx.lineWidth=12;ctx.strokeStyle='#e2e8f0';ctx.stroke();
  [{f:Math.PI,t:Math.PI*1.4,c:'#16a34a'},{f:Math.PI*1.4,t:Math.PI*1.7,c:'#f59e0b'},{f:Math.PI*1.7,t:Math.PI*2,c:'#ef4444'}]
    .forEach(s=>{ctx.beginPath();ctx.arc(cx,cy,r,s.f,s.t,false);ctx.lineWidth=12;ctx.strokeStyle=s.c;ctx.stroke()});
  const ang=Math.PI+(Math.min(score,10)/10)*Math.PI,nL=r*.7;
  ctx.beginPath();ctx.moveTo(cx,cy);ctx.lineTo(cx+Math.cos(ang)*nL,cy+Math.sin(ang)*nL);
  ctx.lineWidth=2.5;ctx.strokeStyle='#1e293b';ctx.lineCap='round';ctx.stroke();
  ctx.beginPath();ctx.arc(cx,cy,4,0,Math.PI*2);ctx.fillStyle='#1e293b';ctx.fill();
  ctx.font=`500 ${Math.round(H*.16)}px Inter,system-ui`;ctx.fillStyle='#94a3b8';ctx.textAlign='center';
  ctx.fillText('0',cx-r-8,cy+2);ctx.fillText('10',cx+r+8,cy+2);
  ctx.font=`700 ${Math.round(H*.27)}px Inter,system-ui`;ctx.fillStyle='#1e293b';ctx.fillText(score.toFixed(1),cx,cy-r*.4);
}

function updateAnalyticsHeader(){
  const header=document.getElementById('analytics-header');
  if(!header)return;
  if(S.mode==='road'){
    if(S.activeSection){
      const sec=S.road&&S.road.sections.find(s=>s.id===S.activeSection);
      const label=sec?sec.label:'';
      header.innerHTML=`Analytics <span class="header-sub">· Section ${S.activeSection} (${label})</span>`;
    }else{
      header.innerHTML=`Analytics <span class="header-sub">· Kamphaeng Phet 6 Corridor</span>`;
    }
  }else{
    header.innerHTML=`Analytics <span class="header-sub">· Full Map Area</span>`;
  }
}

function updateCharts(d){
  updateAnalyticsHeader();
  if(d.before_after){
    document.getElementById('ba-placeholder').style.display='none';
    document.getElementById('ba-row').style.display='flex';
    S.charts.baEvents.data.datasets[0].data=[d.before_after.before.crashes,d.before_after.after.crashes];S.charts.baEvents.update('none');
    S.charts.baRisk.data.datasets[0].data=[d.before_after.before.events,d.before_after.after.events];S.charts.baRisk.update('none');
  }else{
    document.getElementById('ba-placeholder').style.display='block';
    document.getElementById('ba-row').style.display='none';
  }
  if(d.crash_frequency?.length){
    S.charts.freq.data.labels=d.crash_frequency.map(x=>new Date(x.date).toLocaleDateString('en-GB',{month:'short',day:'numeric'}));
    S.charts.freq.data.datasets[0].data=d.crash_frequency.map(x=>x.count);S.charts.freq.update('none');
  }
  if(d.event_breakdown){
    const eb=d.event_breakdown;
    S.charts.events.data.datasets[0].data=[eb.harsh_brake.count,eb.sudden_accel.count,eb.sharp_turn.count,eb.collision.count];
    S.charts.events.update('none');
  }
  // Cache per-day counts keyed by UTC date ("YYYY-MM-DD") — matches the
  // server's date_trunc('day', timestamp) grouping so the join is exact.
  S.dailyCrashes={};S.dailyEvents={};
  (d.crash_frequency||[]).forEach(x=>{S.dailyCrashes[x.date]=x.count});
  (d.daily_events   ||[]).forEach(x=>{S.dailyEvents  [x.date]=x.count});
  S.lastRiskDay=null;          // force refresh on next tick
  updateRiskForCurrentDay();   // initial gauge paint
}

/* ── Per-day risk score ────────────────────────────────── */
const _dayKey=ms=>new Date(ms).toISOString().slice(0,10);

function updateRiskForCurrentDay(force){
  if(!S.dailyCrashes||S.curMs==null)return;
  const day=_dayKey(S.curMs);
  if(!force&&day===S.lastRiskDay)return;
  S.lastRiskDay=day;

  const c=S.dailyCrashes[day]||0;
  const e=S.dailyEvents [day]||0;
  // Same formula as the whole-range version, applied to a single day.
  // (days = 1 → collision_rate = c, event_rate = e)
  const score=Math.min(10, c*10 + e*0.001);
  const rounded=Math.round(score*10)/10;

  drawGauge(rounded);
  const lv=riskLevel(rounded),el=document.getElementById('risk-label');
  el.className='risk-label '+lv.c;el.textContent='RISK: '+lv.l;

  const det=document.getElementById('risk-detail');
  if(det){
    const dateStr=new Date(S.curMs).toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'numeric'});
    det.innerHTML=
      `<b>${dateStr}</b> &nbsp;·&nbsp; Crashes: <b>${c}</b> &nbsp;·&nbsp; Events: <b>${e.toLocaleString()}</b><br>`+
      `<span style="color:#4e7a5c">■ 0–3 Low</span> &nbsp;`+
      `<span style="color:#cf7a3f">■ 3–6 Medium</span> &nbsp;`+
      `<span style="color:#b73d3d">■ 6–10 High</span>`;
  }
}

/* ── Circle ────────────────────────────────────────────── */
function createCircle(lat,lng,radius){
  if(S.circle){S.circle.remove();}
  if(S.mask){S.mask.remove();}
  S.circleCenter=L.latLng(lat,lng);S.radiusM=radius;

  // Dark donut mask — world-spanning outer polygon with a circular hole.
  // Renders in the overlayPane (above tiles, below markers) so vehicle
  // arrows and accident markers always stay visible inside the circle.
  const outerRing=[[-90,-360],[-90,360],[90,360],[90,-360]];
  const innerRing=circleLatLngs(lat,lng,radius);
  S.mask=L.polygon([outerRing,innerRing],{
    color:'none',fillColor:S.mapStyle==='light'?'#faf7f2':'#0f172a',fillOpacity:S.mapStyle==='light'?0.65:0.76,
    interactive:false,smoothFactor:1
  }).addTo(map);

  // Dashed circle border (rendered after mask, so it sits on top)
  S.circle=L.circle([lat,lng],{
    radius,color:'#94a3b8',weight:1.5,dashArray:'6 5',fill:false
  }).addTo(map);
}

let circleDebounce=null;
function onCircleChange(){
  clearTimeout(circleDebounce);
  circleDebounce=setTimeout(async()=>{
    clearAccMarkers();S.accMarkers=[];
    await Promise.allSettled([fetchAccidents(),fetchAnalytics().then(updateCharts)]);
    loadTrajectoryWindow(S.curMs);
  },400);
}

/* ── Timeline ──────────────────────────────────────────── */
function setTime(ms){
  S.curMs=Math.max(S.tStartMs,Math.min(S.tEndMs,ms));
  const pct=(S.curMs-S.tStartMs)/(S.tEndMs-S.tStartMs);
  document.getElementById('timeline').value=Math.round(pct*1000);
  document.getElementById('ts-display').textContent=fmtStamp(S.curMs);
}

function showToast(msg,color){
  let t=document.getElementById('traj-toast');
  if(!t){t=document.createElement('div');t.id='traj-toast';
    t.style.cssText='position:fixed;top:54px;left:50%;transform:translateX(-50%);'
      +'background:#1e293b;color:#f8fafc;font-size:12px;padding:6px 14px;border-radius:6px;'
      +'z-index:9999;pointer-events:none;border:1px solid rgba(255,255,255,.15);transition:opacity .4s';
    document.body.appendChild(t)}
  t.textContent=msg;t.style.borderColor=color||'rgba(255,255,255,.15)';t.style.opacity='1';
  clearTimeout(t._hide);t._hide=setTimeout(()=>{t.style.opacity='0'},4000)}

async function loadTrajectoryWindow(centerMs){
  // Half-window of data to pre-fetch.  Slow playback needs a smaller slice
  // (fewer rows) but still enough to show continuous movement.
  // ≤30s/s → 2 min, ≤1min/s → 5 min, ≤5min/s → 15 min, faster → 30 min.
  const halfWin=S.speedMult<=30?120000:S.speedMult<=60?300000:S.speedMult<=300?900000:1800000;
  const tS=centerMs-halfWin,tE=centerMs+halfWin;
  if(S.cacheStart&&S.cacheEnd&&tS>=S.cacheStart&&tE<=S.cacheEnd)return;
  S.prefetching=true;
  try{
    const d=await fetchTrajectory(tS,tE);
    S.trajs=d.trajectories||{};S.cacheStart=tS;S.cacheEnd=tE;
    const n=Object.keys(S.trajs).length;
    console.log(`[trajectory] ${n} vehicles loaded for window ${new Date(tS).toISOString()} → ${new Date(tE).toISOString()}`);
    if(n===0)showToast('⚠ No vehicle data in this time window — try scrubbing forward','#f59e0b');
  }catch(e){
    console.error('trajectory fetch:',e);
    showToast('✕ Failed to load vehicle data: '+e.message,'#dc2626');
  }
  S.prefetching=false;
}

/* ── Playback ──────────────────────────────────────────── */
function play(){
  if(S.playing)return;S.playing=true;S.lastWall=performance.now();
  document.getElementById('btn-play').classList.add('active');
  document.getElementById('btn-pause').classList.remove('active');
  requestAnimationFrame(tick);
}
function tick(now){
  if(!S.playing)return;
  const wallDelta=now-S.lastWall;S.lastWall=now;
  const dataDelta=wallDelta*S.speedMult;
  const next=S.curMs+dataDelta;
  if(next>=S.tEndMs){setTime(S.tEndMs);pause();renderFrame();return}
  setTime(next);renderFrame();
  if(S.cacheEnd&&S.curMs>S.cacheEnd-60000&&!S.prefetching)loadTrajectoryWindow(S.curMs);
  requestAnimationFrame(tick);
}
function pause(){if(!S.playing)return;S.playing=false;
  document.getElementById('btn-play').classList.remove('active');
  document.getElementById('btn-pause').classList.add('active')}
function stop(){pause();document.getElementById('btn-pause').classList.remove('active');
  setTime(S.tStartMs);S.markers.forEach(m=>m.remove());S.markers.clear();clearAccMarkers();S.accMarkers=[];renderFrame()}

/* ── Collision list + focus mode ───────────────────────── */
function updateTypeFilterOptions(){
  const sel=document.getElementById('collision-type-filter');
  if(!sel)return;
  const prev=sel.value;
  // Collect distinct (type, label) pairs from accidents; fall back to
  // the server-side label dictionary for any types not seen yet.
  const seen=new Map();
  S.accidents.forEach(a=>{
    if(a.collision_type!=null&&!seen.has(String(a.collision_type)))
      seen.set(String(a.collision_type),a.collision_label||('Type '+a.collision_type));
  });
  const opts=['<option value="all">All Types</option>'];
  [...seen.entries()].sort((a,b)=>a[0]-b[0]).forEach(([t,l])=>{
    opts.push(`<option value="${t}">${l.replace(/\s*\(.*?\)\s*/,' ').trim()}</option>`);
  });
  sel.innerHTML=opts.join('');
  sel.value=[...seen.keys(),'all'].includes(prev)?prev:'all';
  S.typeFilter=sel.value;
}

function renderCollisionList(){
  const listEl=document.getElementById('collision-list');
  const countEl=document.getElementById('collision-count');
  if(!listEl)return;

  const filtered=S.accidents.filter(a=>{
    const sevOk=S.severityFilter==='all'||a.severity===S.severityFilter;
    const typeOk=S.typeFilter==='all'||String(a.collision_type)===String(S.typeFilter);
    let roadOk=true;
    if(S.mode==='road'){
      const sec=sectionFor(a.lat,a.lon);
      roadOk=sec!==null&&(!S.activeSection||sec===S.activeSection);
    }
    return sevOk&&typeOk&&roadOk;
  });
  // Newest first — analysts typically want the most recent incident on top
  const sorted=[...filtered].sort((a,b)=>new Date(b.timestamp)-new Date(a.timestamp));
  countEl.textContent=sorted.length;

  if(!sorted.length){
    listEl.innerHTML='<div class="collision-empty">No collisions match the current filters.</div>';
    return;
  }

  listEl.innerHTML=sorted.map(acc=>{
    const origIdx=S.accidents.indexOf(acc);
    const sev=acc.severity||'unknown';
    const d=new Date(acc.timestamp);
    const dateStr=d.toLocaleDateString('en-GB',{day:'2-digit',month:'short'});
    const timeStr=d.toTimeString().slice(0,8);
    const spd=acc.speed!=null?`${Math.round(acc.speed)} km/h`:'—';
    const g=acc.gx!=null?`${Math.abs(acc.gx).toFixed(1)}G`:'—';
    const vinTail=acc.vin?acc.vin.slice(-6):'—';
    const label=(acc.collision_label||'Collision').replace(/\s*\(.*?\)\s*/,' ').trim();
    const isFocused=S.focusedVin===acc.vin&&S.focusCenterMs===d.getTime();
    return `<div class="collision-item${isFocused?' active':''}" data-idx="${origIdx}">
      <span class="collision-dot sev-${sev}"></span>
      <div class="collision-body">
        <div class="collision-title">${label}</div>
        <div class="collision-meta">${dateStr} · ${timeStr}<span class="sep">·</span>${spd}<span class="sep">·</span>${g}
          <div class="collision-vin">VIN ···${vinTail}</div>
        </div>
      </div>
      <span class="collision-severity-tag sev-${sev}">${sev}</span>
    </div>`;
  }).join('');

  listEl.querySelectorAll('.collision-item').forEach(el=>{
    el.addEventListener('click',()=>{
      const idx=parseInt(el.dataset.idx,10);
      focusCollision(idx);
    });
  });
}

async function focusCollision(idx){
  const acc=S.accidents[idx];
  if(!acc)return;
  const tMs=new Date(acc.timestamp).getTime();

  pause();
  setTime(tMs);
  S.focusedVin=acc.vin;
  S.focusCenterMs=tMs;

  const bar=document.getElementById('collision-focus-bar');
  const label=document.getElementById('collision-focus-label');
  bar.classList.remove('hidden');
  label.textContent=`Investigating ···${acc.vin.slice(-6)} · ${new Date(tMs).toTimeString().slice(0,8)} (±${FOCUS_WINDOW_MIN} min)`;

  try{
    const data=await api(`/api/vehicle-trajectory?vin=${encodeURIComponent(acc.vin)}&t_center=${encodeURIComponent(acc.timestamp)}&window_minutes=${FOCUS_WINDOW_MIN}`);
    S.focusWaypoints=data.waypoints||[];
    drawFocusTrack(tMs);
  }catch(e){
    console.error('focus track:',e);
    showToast('✕ Failed to load vehicle track: '+e.message,'#b73d3d');
  }

  // Make sure the broader cache covers this moment so other vehicles render
  S.cacheStart=null;S.cacheEnd=null;
  await loadTrajectoryWindow(S.curMs);
  renderFrame();
  renderCollisionList();
}

function drawFocusTrack(collisionMs){
  // Clear any prior focus polyline + anchor dots
  if(S.focusPolyline){S.focusPolyline.remove();S.focusPolyline=null}
  S.focusMarkers.forEach(m=>m.remove());S.focusMarkers=[];

  const wps=S.focusWaypoints;
  if(!wps||!wps.length)return;
  const latlngs=wps.map(w=>[w.la,w.lo]);

  S.focusPolyline=L.polyline(latlngs,{
    color:'#c7613c',weight:3.5,opacity:.9,
    lineCap:'round',lineJoin:'round',className:'focus-track'
  }).addTo(map);

  // Anchor dots at start / end / impact
  const mk=(latlng,cls,title)=>L.marker(latlng,{
    icon:L.divIcon({className:'',iconSize:[14,14],iconAnchor:[7,7],
      html:`<div class="focus-anchor ${cls}" title="${title}"></div>`}),
    zIndexOffset:2500
  }).addTo(map);
  S.focusMarkers.push(mk(latlngs[0],'start','Track start'));
  S.focusMarkers.push(mk(latlngs[latlngs.length-1],'end','Track end'));

  // Find waypoint closest to collision time and flag it
  let impactIdx=0,best=Infinity;
  wps.forEach((w,i)=>{const d=Math.abs(new Date(w.t).getTime()-collisionMs);if(d<best){best=d;impactIdx=i}});
  S.focusMarkers.push(mk(latlngs[impactIdx],'impact','Impact'));

  if(latlngs.length>1){
    map.fitBounds(S.focusPolyline.getBounds(),{padding:[70,70],maxZoom:18,animate:true});
  }
}

function clearFocus(){
  S.focusedVin=null;S.focusCenterMs=null;S.focusWaypoints=null;
  if(S.focusPolyline){S.focusPolyline.remove();S.focusPolyline=null}
  S.focusMarkers.forEach(m=>m.remove());S.focusMarkers=[];
  document.getElementById('collision-focus-bar').classList.add('hidden');
  S.markers.forEach(m=>{
    const el=m.getElement();
    if(el){el.classList.remove('vehicle-faded');el.classList.remove('vehicle-focused')}
  });
  renderCollisionList();
  renderFrame();
}

function applyFocusStyling(){
  if(!S.focusedVin){
    S.markers.forEach(m=>{
      const el=m.getElement();
      if(el){el.classList.remove('vehicle-faded');el.classList.remove('vehicle-focused')}
    });
    return;
  }
  S.markers.forEach((m,vin)=>{
    const el=m.getElement();
    if(!el)return;
    if(vin===S.focusedVin){
      el.classList.add('vehicle-focused');el.classList.remove('vehicle-faded');
    }else{
      el.classList.add('vehicle-faded');el.classList.remove('vehicle-focused');
    }
  });
}

/* ── Time-jump (exact-time selector) ───────────────────── */
function _pad(n){return String(n).padStart(2,'0')}
function _toLocalDTString(ms){
  const d=new Date(ms);
  return `${d.getFullYear()}-${_pad(d.getMonth()+1)}-${_pad(d.getDate())}T${_pad(d.getHours())}:${_pad(d.getMinutes())}:${_pad(d.getSeconds())}`;
}
function prefillTimeJumpFromCur(){
  const inp=document.getElementById('time-jump-input');
  if(!inp||!S.curMs)return;
  inp.value=_toLocalDTString(S.curMs);
  inp.min=_toLocalDTString(S.tStartMs);
  inp.max=_toLocalDTString(S.tEndMs);
}
function doTimeJump(){
  if(S.staticMode)return;
  const inp=document.getElementById('time-jump-input');
  if(!inp||!inp.value){showToast('Enter a date & time first','#d27a3f');return}
  const target=new Date(inp.value).getTime();
  if(isNaN(target)){showToast('Invalid date/time','#b73d3d');return}
  if(target<S.tStartMs||target>S.tEndMs){
    showToast(`Out of range · dataset is ${fmtStamp(S.tStartMs)} → ${fmtStamp(S.tEndMs)}`,'#d27a3f');
    return;
  }
  pause();
  setTime(target);
  S.cacheStart=null;S.cacheEnd=null;
  loadTrajectoryWindow(S.curMs).then(renderFrame);
  showToast(`→ Jumped to ${fmtStamp(target)}`,'#4a9080');
}

/* ── Road-Focus mode (Kamphaeng Phet 6 Rd) ─────────────── */

const SECTION_COLORS={
  A:{stroke:'#5b8dc4',fillAlpha:.10},   // entry — cool blue
  B:{stroke:'#c7613c',fillAlpha:.10},   // parallel — brand coral
  C:{stroke:'#8b8478',fillAlpha:.06}    // exit — neutral (no data)
};

function clearRoadGeometry(){
  S.roadRects.forEach(r=>r.remove());S.roadRects=[];
  S.roadLabels.forEach(l=>l.remove());S.roadLabels=[];
  if(S.roadMask){S.roadMask.remove();S.roadMask=null}
}

function _polygonCentroid(poly){
  let lat=0,lon=0;
  poly.forEach(p=>{lat+=p[0];lon+=p[1]});
  return[lat/poly.length,lon/poly.length];
}

function drawRoadGeometry(){
  clearRoadGeometry();
  if(!S.road)return;

  // When a section is pinned, only that section is visible — the others are
  // hidden entirely (per user request: "only show the selected part").
  const visibleSections=S.activeSection
    ? S.road.sections.filter(s=>s.id===S.activeSection)
    : S.road.sections;

  // Mask: world polygon with one hole per *visible* section so only those
  // areas stay bright; everything else is dimmed.
  const outerRing=[[-90,-360],[-90,360],[90,360],[90,-360]];
  const holes=visibleSections.map(s=>s.polygon||[
    [s.lat_min,s.lon_min],[s.lat_min,s.lon_max],
    [s.lat_max,s.lon_max],[s.lat_max,s.lon_min]
  ]);
  S.roadMask=L.polygon([outerRing,...holes],{
    color:'none',fillColor:S.mapStyle==='light'?'#faf7f2':'#0f0d0b',fillOpacity:S.mapStyle==='light'?.62:.78,
    interactive:false,smoothFactor:1
  }).addTo(map);

  // Section polygons + tooltips
  visibleSections.forEach(s=>{
    const c=SECTION_COLORS[s.id]||SECTION_COLORS.A;
    const isActive=S.activeSection===s.id;
    const ring=s.polygon||[
      [s.lat_min,s.lon_min],[s.lat_min,s.lon_max],
      [s.lat_max,s.lon_max],[s.lat_max,s.lon_min]
    ];
    const rect=L.polygon(ring,{
      color:c.stroke,
      weight:isActive?2.4:1.6,
      fillColor:c.stroke,
      fillOpacity:c.fillAlpha,
      fill:true,
      dashArray:isActive?null:'4 6',
      className:`section-rect section-${s.id}${isActive?' active':''}`
    }).addTo(map);
    rect.bindTooltip(`Section ${s.id} · ${s.label}`,{direction:'top',sticky:true,className:'section-tooltip'});
    
    // Only allow selecting sections by clicking on the map if in 'road' mode
    if(S.mode==='road'){
      rect.on('click',()=>setActiveSection(S.activeSection===s.id?null:s.id));
    }
    S.roadRects.push(rect);
  });
}

function setActiveSection(id){
  S.activeSection=id;
  drawRoadGeometry();                     // re-paint (other sections hidden)

  // Mark the active tile + sync collision-list scope
  document.querySelectorAll('.section-tile').forEach(el=>{
    el.classList.toggle('active',el.dataset.section===id);
  });

  // On-map close button + zoom
  const closeBtn=document.getElementById('btn-close-section');
  if(id){
    const sec=S.road.sections.find(s=>s.id===id);
    if(sec){
      const ring=sec.polygon||[
        [sec.lat_min,sec.lon_min],[sec.lat_max,sec.lon_max]
      ];
      map.fitBounds(ring,{padding:[40,40],animate:true,maxZoom:19});
    }
    if(closeBtn){
      closeBtn.classList.remove('hidden');
      const sec2=S.road.sections.find(s=>s.id===id);
      closeBtn.setAttribute('title',`Back to full road view (currently focused on Section ${id} · ${sec2?sec2.label:''})`);
    }
  }else{
    // Restore the full road view
    const bb=roadBbox();
    map.fitBounds([[bb.lat_min,bb.lon_min],[bb.lat_max,bb.lon_max]],{padding:[60,60],animate:true});
    if(closeBtn)closeBtn.classList.add('hidden');
  }

  // Bbox changed → invalidate caches and refetch the scope-dependent feeds
  S.cacheStart=null;S.cacheEnd=null;
  S.lastRiskDay=null;     // sidebar gauge will repaint for new scope
  Promise.allSettled([fetchAccidents(),fetchAnalytics().then(updateCharts)])
    .then(()=>loadTrajectoryWindow(S.curMs).then(renderFrame));
}

async function enterRoadMode(){
  if(!S.road){
    try{S.road=await api('/api/road')}
    catch(e){showToast('Failed to load road config: '+e.message,'#b73d3d');return}
  }
  S.mode='road';S.activeSection=null;
  document.getElementById('btn-mode-full').classList.remove('active');
  document.getElementById('btn-mode-road').classList.add('active');
  document.getElementById('section-panel').classList.remove('hidden');

  // Hide the circle/donut from Full mode, draw section rectangles + mask
  if(S.circle){S.circle.remove();S.circle=null}
  if(S.mask){S.mask.remove();S.mask=null}
  drawRoadGeometry();

  // Fit the map to the union of all sections
  const bb=roadBbox();
  map.fitBounds([[bb.lat_min,bb.lon_min],[bb.lat_max,bb.lon_max]],{padding:[60,60]});

  // Reset caches and refetch everything against the new bbox
  S.cacheStart=null;S.cacheEnd=null;S.lastRiskDay=null;
  renderSectionTiles();   // skeleton tiles immediately so the panel isn't empty
  await Promise.allSettled([
    fetchAccidents(),
    fetchAnalytics().then(updateCharts),
    fetchSectionAnalytics()
  ]);
  await loadTrajectoryWindow(S.curMs);
  renderFrame();
}

async function exitRoadMode(){
  S.mode='full';S.activeSection=null;
  document.getElementById('btn-mode-road').classList.remove('active');
  document.getElementById('btn-mode-full').classList.add('active');
  document.getElementById('section-panel').classList.add('hidden');
  document.getElementById('btn-close-section').classList.add('hidden');
  clearRoadGeometry();

  // Restore the circle from blackspots.json
  if(S.bounds){
    let lat=(S.bounds.lat_min+S.bounds.lat_max)/2,
        lng=(S.bounds.lon_min+S.bounds.lon_max)/2,r=500;
    try{const bs=await api('/api/blackspots');if(bs&&bs.length){lat=bs[0].lat;lng=bs[0].lon;r=bs[0].radius_m||500}}catch(e){}
    createCircle(lat,lng,r);
    map.fitBounds([[S.bounds.lat_min,S.bounds.lon_min],[S.bounds.lat_max,S.bounds.lon_max]],{padding:[30,30]});
  }
  S.cacheStart=null;S.cacheEnd=null;S.lastRiskDay=null;
  await Promise.allSettled([fetchAccidents(),fetchAnalytics().then(updateCharts)]);
  await loadTrajectoryWindow(S.curMs);
  renderFrame();
}

/* ── Per-section analytics tiles ───────────────────────── */
function renderSectionTiles(){
  const grid=document.getElementById('section-grid');
  if(!grid||!S.road)return;
  grid.innerHTML=S.road.sections.map(s=>{
    const c=SECTION_COLORS[s.id]||SECTION_COLORS.A;
    const a=S.sectionAnalytics[s.id];
    const isLoading=a&&a._loading;
    const isError=a&&a._error;
    const eb=a&&!isLoading&&!isError&&a.event_breakdown||null;
    const totalCrashes=eb?(eb.collision?.count||0):null;
    const totalEvents =eb?((eb.harsh_brake?.count||0)+(eb.sudden_accel?.count||0)+(eb.sharp_turn?.count||0)):null;
    const noData=eb&&totalCrashes===0&&totalEvents===0;
    let bodyHtml;
    if(isLoading){
      bodyHtml=`<div class="section-tile-empty"><div class="section-spinner"></div>Loading analytics…</div>`;
    }else if(isError){
      bodyHtml=`<div class="section-tile-empty section-tile-error">Failed to load<div class="section-tile-empty-sub">${a._error}</div></div>`;
    }else if(noData){
      bodyHtml=`<div class="section-tile-empty">No data in this window<div class="section-tile-empty-sub">Pending data extraction over Kamphaeng Phet 6 Rd</div></div>`;
    }else{
      bodyHtml=`<div class="section-tile-stats">
        <div class="stat"><div class="stat-num" data-sec="${s.id}" data-stat="crashes">${totalCrashes!=null?totalCrashes:'—'}</div><div class="stat-lbl">Total<br>crashes</div></div>
        <div class="stat"><div class="stat-num" data-sec="${s.id}" data-stat="events">${totalEvents!=null?totalEvents.toLocaleString():'—'}</div><div class="stat-lbl">Total<br>events</div></div>
        <div class="stat"><div class="stat-num stat-risk" data-sec="${s.id}" data-stat="risk-today">—</div><div class="stat-lbl">Risk<br>today</div></div>
      </div>
      <div class="section-tile-day" data-sec="${s.id}" data-stat="day-detail">— · — crashes · — events</div>`;
    }
    return `<button class="section-tile${S.activeSection===s.id?' active':''}${noData?' no-data':''}${isError?' has-error':''}" data-section="${s.id}" style="--c:${c.stroke}">
      <div class="section-tile-head">
        <span class="section-tile-id">${s.id}</span>
        <span class="section-tile-label">${s.label}</span>
      </div>
      ${bodyHtml}
    </button>`;
  }).join('');
  grid.querySelectorAll('.section-tile').forEach(el=>{
    el.addEventListener('click',()=>{
      const id=el.dataset.section;
      setActiveSection(S.activeSection===id?null:id);
    });
  });
}

function updateSectionRiskForCurrentDay(force){
  if(!S.road||S.curMs==null)return;
  const day=_dayKey(S.curMs);
  S.road.sections.forEach(s=>{
    if(!force&&S.lastSectionRiskDay[s.id]===day)return;
    S.lastSectionRiskDay[s.id]=day;
    const dd=S.sectionDaily[s.id];
    if(!dd)return;
    const c=dd.crashes[day]||0;
    const e=dd.events[day]||0;
    const score=Math.min(10,c*10+e*0.001);
    const rounded=Math.round(score*10)/10;
    const lvl=score<=3?'low':score<=6?'medium':'high';
    const riskEl=document.querySelector(`.section-tile[data-section="${s.id}"] [data-stat="risk-today"]`);
    if(riskEl){
      riskEl.textContent=rounded.toFixed(1);
      riskEl.classList.remove('risk-low','risk-medium','risk-high');
      riskEl.classList.add('risk-'+lvl);
    }
    const dayEl=document.querySelector(`.section-tile[data-section="${s.id}"] [data-stat="day-detail"]`);
    if(dayEl){
      const dateStr=new Date(S.curMs).toLocaleDateString('en-GB',{day:'2-digit',month:'short'});
      dayEl.innerHTML=`<b>${dateStr}</b> · ${c} crash${c===1?'':'es'} · ${e.toLocaleString()} event${e===1?'':'s'}`;
    }
  });
}

/* ── Heatmap mode ──────────────────────────────────────── */

// Analysis zone: user-specified corridor along Kamphaeng Phet 6 Rd.
// Centerline: Start (13.8390065, 100.556055) → End (13.8412935, 100.557258).
// Corridor half-width: ±17.25 m.
const HEATMAP_ZONE = (() => {
  const S0 = [13.8390065, 100.556055], E0 = [13.8412935, 100.557258];
  const mLat = 111000, mLon = 111000 * Math.cos(13.8396 * Math.PI / 180);
  const dx = (E0[0] - S0[0]) * mLat, dy = (E0[1] - S0[1]) * mLon;
  const len = Math.sqrt(dx * dx + dy * dy);
  const ux = dx / len, uy = dy / len;   // unit along road (N,E)
  const vx = -uy,     vy = ux;          // perpendicular (left, right)
  const W = 17.25;                       // half-width metres
  const corners = [
    [S0[0] + (vx * W) / mLat, S0[1] + (vy * W) / mLon],
    [E0[0] + (vx * W) / mLat, E0[1] + (vy * W) / mLon],
    [E0[0] - (vx * W) / mLat, E0[1] - (vy * W) / mLon],
    [S0[0] - (vx * W) / mLat, S0[1] - (vy * W) / mLon],
  ];
  const lats = corners.map(c => c[0]), lons = corners.map(c => c[1]);
  return {
    polygon: corners,
    bbox: { lat_min: Math.min(...lats), lat_max: Math.max(...lats),
            lon_min: Math.min(...lons), lon_max: Math.max(...lons) },
    bearing: Math.round(Math.atan2(dy, dx) * 180 / Math.PI),
    center: [(S0[0] + E0[0]) / 2, (S0[1] + E0[1]) / 2],
  };
})();

// Colour gradients per event type — passed to L.heatLayer's gradient option.
const HEAT_GRADIENTS = {
  0: {0.2:'#3b82f6', 0.5:'#f59e0b', 1.0:'#dc2626'},              // all  → blue-amber-red
  1: {0.2:'#fef3c7', 0.5:'#f59e0b', 1.0:'#b45309'},              // brake → amber/dark-amber
  2: {0.2:'#ccfbf1', 0.5:'#0d9488', 1.0:'#065f46'},              // accel → teal
  3: {0.2:'#ede9fe', 0.5:'#8b5cf6', 1.0:'#5b21b6'},              // turn  → purple
};

async function fetchHeatmapData(){
  const bb = HEATMAP_ZONE.bbox;
  const hour = S.heatHour === 24 ? -1 : S.heatHour;
  const p = new URLSearchParams({
    lat_min: bb.lat_min, lat_max: bb.lat_max,
    lon_min: bb.lon_min, lon_max: bb.lon_max,
    event_type:    S.heatEventType,
    speed_bracket: S.heatSpeedBracket,
    hour,
  });
  const loadEl = document.getElementById('hm-loading');
  const cntEl  = document.getElementById('hm-point-count');
  if(loadEl) loadEl.classList.remove('hidden');
  try {
    const d = await api('/api/heatmap?' + p);
    renderHeatLayer(d.points);
    if(cntEl){
      cntEl.textContent = d.total.toLocaleString() + ' event' + (d.total !== 1 ? 's' : '');
      if(d.capped) cntEl.textContent += ' (capped at 60 000)';
    }
  } catch(e) {
    console.error('[heatmap] fetch failed:', e);
    showToast('Heatmap fetch failed: ' + e.message, '#b73d3d');
    if(cntEl) cntEl.textContent = 'Failed to load';
  } finally {
    if(loadEl) loadEl.classList.add('hidden');
  }
}

function renderHeatLayer(points){
  if(S.heatLayer){ S.heatLayer.remove(); S.heatLayer = null; }
  if(!points || !points.length) return;

  // Create custom heatmap pane on top of SVG overlay pane (z-index 450)
  // to prevent it from being occluded/dimmed by the dark road mask
  if(!map.getPane('heatmapPane')){
    const pane = map.createPane('heatmapPane');
    pane.style.zIndex = 450;
    pane.style.pointerEvents = 'none';
  }

  const grad = HEAT_GRADIENTS[S.heatEventType] || HEAT_GRADIENTS[0];
  
  // Reduce density saturation by lowering radius/blur and raising max capacity
  S.heatLayer = L.heatLayer(points, {
    radius:      10,      // reduced from 18 to isolate hotspots
    blur:        8,       // reduced from 14 for crisper boundaries
    maxZoom:     19,
    max:         30.0,    // scaled up from 1.0 to prevent immediate red-saturation
    gradient:    grad,
    minOpacity:  0.15,    // lowered from 0.35 to blend low-density margins smoothly
    pane:        'heatmapPane', // draw on top of the mask!
  }).addTo(map);
}

function clearHeatLayer(){
  if(S.heatLayer){ S.heatLayer.remove(); S.heatLayer = null; }
  if(S.heatZonePoly){ S.heatZonePoly.remove(); S.heatZonePoly = null; }
}

async function enterHeatmapMode(){
  S.mode = 'heatmap';
  document.getElementById('heatmap-panel').classList.remove('hidden');
  document.getElementById('btn-close-section').classList.add('hidden');
  pause();

  // Ensure road config is loaded
  if(!S.road){
    try {
      S.road = await api('/api/road');
    } catch(e) {
      showToast('Failed to load road config: ' + e.message, '#b73d3d');
      return;
    }
  }

  // Draw the 3-section polygons and the roadMask
  drawRoadGeometry();

  // Zoom to the unified road bounds
  const bb = roadBbox();
  map.fitBounds([[bb.lat_min, bb.lon_min], [bb.lat_max, bb.lon_max]], {padding:[80,80], maxZoom:18, animate:true});

  fetchHeatmapData();
}

async function exitHeatmapMode(){
  clearHeatLayer();
  clearRoadGeometry();
  document.getElementById('heatmap-panel').classList.add('hidden');
}

/* ── Init ──────────────────────────────────────────────── */
async function init(){
  const loadEl=document.getElementById('loading'),msg=document.getElementById('load-msg'),err=document.getElementById('load-err');
  try{
    initCharts();
    msg.textContent='Loading dataset metadata…';
    const meta=await api('/api/meta');
    if(!meta.t_start)throw new Error('Empty dataset');
    S.tStartMs=new Date(meta.t_start).getTime();
    S.tEndMs=new Date(meta.t_end).getTime();

    // Start at the busiest window the server found, NOT at tStart.
    // tStart is often a low-traffic period (e.g. midnight) and the user
    // would see nothing for minutes. t_suggested points to a 10-min peak.
    // Fall back to the date-range midpoint if the server didn't supply it.
    const suggestedMs = meta.t_suggested
      ? new Date(meta.t_suggested).getTime()
      : Math.round((S.tStartMs + S.tEndMs) / 2);
    S.curMs = Math.max(S.tStartMs, Math.min(S.tEndMs, suggestedMs));

    S.eventLabels=meta.event_labels||{};S.collisionLabels=meta.collision_labels||{};
    S.bounds=meta.bounds;
    document.getElementById('tl-start').textContent=fmtDate(S.tStartMs);
    document.getElementById('tl-end').textContent=fmtDate(S.tEndMs);
    document.getElementById('ts-display').textContent=fmtStamp(S.curMs);

    const b=S.bounds;
    // Map panning is enabled — users can drag freely.
    map.setMinZoom(13);
    // Fit view to the full dataset bounds so users see the whole study area.
    map.fitBounds([[b.lat_min,b.lon_min],[b.lat_max,b.lon_max]],{padding:[30,30]});

    // Place the analysis circle on the first configured blackspot (road
    // intersection), not on the geometric centre of the data bounding box.
    let circleLat=(b.lat_min+b.lat_max)/2,circleLng=(b.lon_min+b.lon_max)/2,circleR=500;
    try{
      const bs=await api('/api/blackspots');
      if(bs&&bs.length){circleLat=bs[0].lat;circleLng=bs[0].lon;circleR=bs[0].radius_m||500;}
    }catch(e){console.warn('Blackspots endpoint unavailable:',e)}
    createCircle(circleLat,circleLng,circleR);

    // Load Kamphaeng Phet 6 Rd config in parallel so the toggle is instant
    api('/api/road').then(r=>{S.road=r}).catch(()=>{});

    msg.textContent='Loading accidents…';
    await fetchAccidents();
    msg.textContent='Loading analytics…';
    const ad=await fetchAnalytics();updateCharts(ad);

    // Load trajectory window; if still empty, scan forward in 2-hour steps
    // until we find data or exhaust the date range.
    msg.textContent='Finding vehicle data…';
    await loadTrajectoryWindow(S.curMs);
    if(Object.keys(S.trajs).length===0){
      const stepMs=2*3600*1000; // 2 hours of data time
      let probe=S.tStartMs;
      while(probe<=S.tEndMs && Object.keys(S.trajs).length===0){
        probe+=stepMs;
        S.curMs=Math.min(probe,S.tEndMs);
        // Reset cache so the window is actually fetched
        S.cacheStart=null;S.cacheEnd=null;
        msg.textContent=`Scanning for traffic… ${fmtStamp(S.curMs)}`;
        await loadTrajectoryWindow(S.curMs);
      }
    }
    setTime(S.curMs); // sync timeline slider to wherever we landed
    prefillTimeJumpFromCur();

    loadEl.classList.add('hidden');
    play();
  }catch(e){msg.style.display='none';err.style.display='block';err.textContent='Error: '+e.message;console.error(e)}
}

/* ── Events ────────────────────────────────────────────── */
// radius-slider removed — circle is fixed to the blackspot radius from blackspots.json
document.getElementById('btn-reset').addEventListener('click',async()=>{
  if(S.mode==='road'){
    // Re-fit to the road and drop any pinned section
    const bb=S.road?roadBbox():null;
    if(bb)map.fitBounds([[bb.lat_min,bb.lon_min],[bb.lat_max,bb.lon_max]],{padding:[60,60]});
    if(S.activeSection){setActiveSection(null)}
    return;
  }
  if(!S.bounds)return;
  const b=S.bounds;
  map.fitBounds([[b.lat_min,b.lon_min],[b.lat_max,b.lon_max]],{padding:[30,30]});
  // Re-snap circle to blackspot
  let lat=(b.lat_min+b.lat_max)/2,lng=(b.lon_min+b.lon_max)/2,r=500;
  try{const bs=await api('/api/blackspots');if(bs&&bs.length){lat=bs[0].lat;lng=bs[0].lon;r=bs[0].radius_m||500;}}catch(e){}
  createCircle(lat,lng,r);onCircleChange();
});
document.getElementById('btn-acc-mode').addEventListener('click',function(){
  const modes=['persist','flash','hidden'],cur=modes.indexOf(S.accMode);
  S.accMode=modes[(cur+1)%3];
  this.textContent={'persist':'Persist','flash':'Flash','hidden':'Hidden'}[S.accMode];
  // Wipe stale markers so e.g. Persist→Flash actually drops the ones outside
  // the flash window when playback is paused.
  clearAccMarkers();S.accMarkers=[];
  if(S.accMode!=='hidden')renderAccidents();
});
function setTimelineInteraction(enabled){
  const ids = ['timeline','btn-pause','btn-play','btn-stop','btn-prev','btn-next','time-jump-input','btn-time-jump','speed-select'];
  ids.forEach(id=>{
    const el=document.getElementById(id);
    if(!el)return;
    if(enabled){
      el.removeAttribute('disabled');
      el.classList.remove('disabled');
    }else{
      el.setAttribute('disabled','true');
      el.classList.add('disabled');
    }
  });
}
document.getElementById('btn-toggle-static').addEventListener('click',function(){
  S.staticMode=!S.staticMode;
  if(S.staticMode){
    this.classList.add('active');
    this.textContent='Active Data: Hidden';
    pause();
    setTimelineInteraction(false);
  }else{
    this.classList.remove('active');
    this.textContent='Hide Active Data';
    setTimelineInteraction(true);
  }
  renderFrame();
});
document.getElementById('timeline').addEventListener('input',e=>{
  if(S.staticMode)return;
  const pct=e.target.value/1000;S.curMs=S.tStartMs+pct*(S.tEndMs-S.tStartMs);
  document.getElementById('ts-display').textContent=fmtStamp(S.curMs);
  clearTimeout(S._slTm);S._slTm=setTimeout(()=>{loadTrajectoryWindow(S.curMs).then(renderFrame)},300)});
document.getElementById('btn-play').addEventListener('click',()=>{if(!S.staticMode)play()});
document.getElementById('btn-pause').addEventListener('click',()=>{if(!S.staticMode)pause()});
document.getElementById('btn-stop').addEventListener('click',()=>{if(!S.staticMode)stop()});
document.getElementById('btn-prev').addEventListener('click',()=>{if(S.staticMode)return;pause();setTime(S.curMs-S.speedMult*1000);loadTrajectoryWindow(S.curMs).then(renderFrame)});
document.getElementById('btn-next').addEventListener('click',()=>{if(S.staticMode)return;pause();setTime(S.curMs+S.speedMult*1000);loadTrajectoryWindow(S.curMs).then(renderFrame)});
document.getElementById('speed-select').addEventListener('change',function(){
  S.speedMult=parseInt(this.value);
  // Force a fresh trajectory fetch since sample_sec may have changed
  S.cacheStart=null;S.cacheEnd=null;
  loadTrajectoryWindow(S.curMs);
});
document.getElementById('cm-apply').addEventListener('click',async()=>{
  try{const d=await fetchAnalytics();updateCharts(d)}catch(e){console.error(e)}});
function toggleMapStyle(){
  const to=S.mapStyle==='dark'?'light':'dark';
  S.mapStyle=to;
  map.removeLayer(baseTileLayer);
  const url=to==='light'
    ? 'https://{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}'
    : 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png';
  const subdomains=to==='light'?['mt0','mt1','mt2','mt3']:'abcd';
  baseTileLayer=L.tileLayer(url,{
    attribution:to==='light'?'&copy; Google':'&copy; OSM &copy; CARTO',
    subdomains:subdomains,maxZoom:20}).addTo(map);
  baseTileLayer.bringToBack();
  if(S.mode==='road'){
    drawRoadGeometry();
  }else if(S.mode==='full'){
    if(S.circleCenter){
      createCircle(S.circleCenter.lat,S.circleCenter.lng,S.radiusM);
    }
  }
  const btn=document.getElementById('btn-map-toggle');
  if(btn){
    btn.textContent=to==='light'?'🗺️ Google Map':'🌙 Dark Mode';
    btn.classList.toggle('active',to==='light');
  }
}
document.getElementById('btn-map-toggle').addEventListener('click',toggleMapStyle);

/* Collision panel wiring */
document.getElementById('collision-severity-filter').addEventListener('change',function(){
  S.severityFilter=this.value;
  renderCollisionList();
  // Filter also applies to map markers — re-draw
  clearAccMarkers();S.accMarkers=[];renderAccidents();
});
document.getElementById('collision-type-filter').addEventListener('change',function(){
  S.typeFilter=this.value;
  renderCollisionList();
  clearAccMarkers();S.accMarkers=[];renderAccidents();
});
document.getElementById('btn-collapse-collisions').addEventListener('click',function(){
  S.panelCollapsed=!S.panelCollapsed;
  document.getElementById('collision-panel').classList.toggle('collapsed',S.panelCollapsed);
  this.textContent=S.panelCollapsed?'▶':'◀';
  // Leaflet needs a nudge to recalc size after the panel width changes
  setTimeout(()=>map.invalidateSize(),260);
});
document.getElementById('btn-clear-focus').addEventListener('click',clearFocus);

/* Unified mode switcher — handles any → any transition cleanly */
async function switchMode(to){
  if(S.mode===to)return;
  const from=S.mode;
  // --- tear-down current mode ---
  if(from==='heatmap') await exitHeatmapMode();
  if(from==='road') { clearRoadGeometry(); document.getElementById('section-panel').classList.add('hidden'); S.activeSection=null; }
  if(from==='full'||from==='road') { if(S.circle){S.circle.remove();S.circle=null} if(S.mask){S.mask.remove();S.mask=null} }
  // always clear vehicle markers between modes
  S.markers.forEach(m=>m.remove()); S.markers.clear();
  clearAccMarkers(); S.accMarkers=[];
  // --- set-up new mode ---
  if(to==='full') await exitRoadMode();        // restores circle + refetches
  else if(to==='road') await enterRoadMode();
  else if(to==='heatmap') await enterHeatmapMode();
  // sync button states
  document.getElementById('btn-mode-full').classList.toggle('active', to==='full');
  document.getElementById('btn-mode-road').classList.toggle('active', to==='road');
  document.getElementById('btn-mode-heatmap').classList.toggle('active', to==='heatmap');
}

document.getElementById('btn-mode-full').addEventListener('click',     ()=>switchMode('full'));
document.getElementById('btn-mode-road').addEventListener('click',     ()=>switchMode('road'));
document.getElementById('btn-mode-heatmap').addEventListener('click',  ()=>switchMode('heatmap'));

/* Heatmap filter controls */
document.querySelectorAll('#hm-event-pills .hm-pill').forEach(btn=>{
  btn.addEventListener('click',()=>{
    document.querySelectorAll('#hm-event-pills .hm-pill').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    S.heatEventType=parseInt(btn.dataset.value,10);
    fetchHeatmapData();
  });
});
document.querySelectorAll('#hm-speed-pills .hm-pill').forEach(btn=>{
  btn.addEventListener('click',()=>{
    document.querySelectorAll('#hm-speed-pills .hm-pill').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    S.heatSpeedBracket=parseInt(btn.dataset.value,10);
    fetchHeatmapData();
  });
});
document.getElementById('hm-hour-slider').addEventListener('input',function(){
  S.heatHour=parseInt(this.value,10);
  const disp=document.getElementById('hm-hour-display');
  if(disp) disp.textContent=S.heatHour===24?'All hours':
    String(S.heatHour).padStart(2,'0')+':00 – '+String(S.heatHour).padStart(2,'0')+':59';
});
document.getElementById('hm-hour-slider').addEventListener('change',()=>fetchHeatmapData());

/* On-map "Back to full road" close button — exits the active-section zoom */
document.getElementById('btn-close-section').addEventListener('click',()=>{
  if(S.activeSection)setActiveSection(null);
});

/* Time-jump wiring (Go button + Enter key) */
document.getElementById('btn-time-jump').addEventListener('click',doTimeJump);
document.getElementById('time-jump-input').addEventListener('keydown',e=>{
  if(e.key==='Enter'){e.preventDefault();doTimeJump();}
});

init();
