'use strict';
const byId = id => document.getElementById(id);
const services = {apple:'Apple',gamecenter:'Apple',steam:'Steam',psn:'PSN',xbox:'Xbox',nintendo:'Nintendo',gog:'GOG',epic:'Epic',retro:'RetroAchievements',retroachievements:'RetroAchievements'};
const serviceName = value => services[value] || value || 'Unknown';
const number = (value, digits=0) => new Intl.NumberFormat('en-AU',{maximumFractionDigits:digits}).format(value);
const date = value => new Intl.DateTimeFormat('en-AU',{dateStyle:'medium',timeStyle:'short',timeZone:'Australia/Melbourne'}).format(new Date(value)) + ' Melbourne';
let payload = null;
function element(tag, text, className) {const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;}
function validNumber(value) {return typeof value==='number' && Number.isFinite(value) && value>=0;}
function validate(data) {
  if(data?.schema!=='selfexophase-telemetry/v1'||!Array.isArray(data.games)||!data.metadata||data.metadata.total_games!==data.games.length)throw new Error('The dataset format is not supported.');
  const ids=new Set();
  for(const game of data.games){
    if(typeof game.id!=='string'||ids.has(game.id)||typeof game.title!=='string'||typeof game.service!=='string'||!Array.isArray(game.platforms)||game.platforms.some(p=>typeof p!=='string'))throw new Error('A source record could not be read.');
    if(game.hours!==null&&!validNumber(game.hours))throw new Error('Playtime data is invalid.');
    if(game.achievement_completion_percent!==null&&(!validNumber(game.achievement_completion_percent)||game.achievement_completion_percent>100))throw new Error('Achievement data is invalid.');
    ids.add(game.id);
  }
  if(!validNumber(data.metadata.total_known_hours)||!validNumber(data.metadata.unknown_playtime_count))throw new Error('The activity summary could not be read.');
  if(!Number.isFinite(Date.parse(data.metadata.last_updated)))throw new Error('The dataset timestamp is invalid.');
  if(data.metadata.source_retrieved_at!==null&&!Number.isFinite(Date.parse(data.metadata.source_retrieved_at)))throw new Error('The source timestamp is invalid.');
}
function renderSummary() {
  const m=payload.metadata;
  byId('stat-hours').textContent=number(m.total_known_hours,1);
  byId('stat-count').textContent=number(payload.games.length);
  byId('stat-achievements').textContent=number(payload.games.filter(g=>g.achievement_completion_percent===100).length);
  byId('hours-note').textContent=`${number(m.unknown_playtime_count)} games have unknown playtime`;
  byId('services-note').textContent=`${new Set(payload.games.map(g=>g.service)).size} services with activity`;
  byId('sync-time').textContent=m.source_retrieved_at?`Downloaded ${date(m.source_retrieved_at)}`:'Offline import · live download time unknown';
  byId('generated-time').textContent=`Report generated ${date(m.last_updated)}`;
  const stale=!m.source_retrieved_at||Date.now()-Date.parse(m.source_retrieved_at)>24*60*60*1000;
  byId('sync-indicator').className='indicator '+(stale?'stale':'ready');
  byId('freshness-note').hidden=!stale;
  byId('freshness-note').textContent=m.source_retrieved_at?'This snapshot is more than 24 hours old. The last successful download remains available.':'This is an offline snapshot. Report generation time does not establish when the source was downloaded.';
  const current=byId('service').value;
  byId('service').replaceChildren(new Option('All services',''),...Array.from(new Set(['steam','nintendo','psn','apple','xbox','retro','gog','epic',...payload.games.map(g=>g.service)])).sort((a,b)=>serviceName(a).localeCompare(serviceName(b))).map(s=>new Option(serviceName(s),s)));
  byId('service').value=current;
}
function renderRows() {
  if(!payload)return;
  const query=byId('search').value.trim().toLocaleLowerCase(),service=byId('service').value,sort=byId('sort').value;
  const games=payload.games.filter(g=>(!service||g.service===service)&&(!query||[g.title,...g.platforms].join(' ').toLocaleLowerCase().includes(query)));
  games.sort((a,b)=>sort==='title'?a.title.localeCompare(b.title):sort==='achievements'?(b.achievement_completion_percent??-1)-(a.achievement_completion_percent??-1)||a.title.localeCompare(b.title):(b.hours??-1)-(a.hours??-1)||a.title.localeCompare(b.title));
  byId('library-stream').replaceChildren(...games.map(game=>{
    const row=element('tr'),title=element('td',undefined,'game-title');
    let url=null;try{const candidate=new URL(game.source_url);if(candidate.protocol==='https:'&&(candidate.hostname==='exophase.com'||candidate.hostname.endsWith('.exophase.com')))url=candidate.href;}catch{}
    if(url){const link=element('a',game.title);link.href=url;link.target='_blank';link.rel='noreferrer noopener';title.append(link);}else title.textContent=game.title;
    const serviceCell=element('td');serviceCell.append(element('span',serviceName(game.service),'service-tag'));
    const platforms=element('td',game.platforms.join(', ')||'Unknown','source-platforms');
    const playtime=element('td',game.hours===null?'Unknown':game.hours>0&&game.hours<1?`${number(game.playtime_minutes??game.hours*60)} min`:`${number(game.hours,1)} h`,'number'+(game.hours===null?' unknown':''));
    const percent=game.achievement_completion_percent;
    const achievement=element('td',percent===null?'Unknown':`${number(percent,1)}%`,'number'+(percent===100?' achievement-complete':percent===null?' unknown':''));
    if(percent!==null){const track=element('div',undefined,'achievement-track'),fill=element('div',undefined,'achievement-fill');fill.style.width=`${percent}%`;track.setAttribute('aria-hidden','true');track.append(fill);achievement.append(track);}
    row.append(title,serviceCell,platforms,playtime,achievement);return row;
  }));
  byId('result-count').textContent=`${number(games.length)} of ${number(payload.games.length)} source records`;
  byId('empty').hidden=games.length>0;
}
async function load() {
  byId('retry').disabled=true;
  try{
    const response=await fetch('output/data.json',{cache:'no-store'});
    if(!response.ok)throw new Error(`The data request failed (HTTP ${response.status}).`);
    const data=await response.json();validate(data);payload=data;
    renderSummary();renderRows();byId('load-error').hidden=true;
  }catch(error){byId('load-error').hidden=false;byId('error-text').textContent=(payload?'Showing the previously loaded snapshot. ':'')+(error instanceof SyntaxError?'The activity JSON could not be read.':error.message);if(!payload)byId('result-count').textContent='Data unavailable';}
  finally{byId('retry').disabled=false;}
}
for(const id of ['search','service','sort'])byId(id).addEventListener('input',renderRows);
byId('retry').addEventListener('click',load);
load();
