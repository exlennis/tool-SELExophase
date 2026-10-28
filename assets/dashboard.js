'use strict';
const {serviceName, validate, prepare, achievementView, filterAndSort, serviceCounts, summarise} = ExophaseLogic;
const byId = id => document.getElementById(id);
const number = (value, digits=0) => new Intl.NumberFormat('en-AU',{maximumFractionDigits:digits}).format(value);
const zone = 'Australia/Melbourne';
const dateTimeFormat = new Intl.DateTimeFormat('en-AU',{dateStyle:'medium',timeStyle:'short',timeZone:zone});
const dayFormat = new Intl.DateTimeFormat('en-AU',{dateStyle:'medium',timeZone:zone});
const date = value => dateTimeFormat.format(new Date(value)) + ' Melbourne';
let payload = null, games = [], searchTimer = 0;
function element(tag, text, className) {const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;}
function placeholder(game) {const node=element('div',Array.from(game.title)[0]?.toLocaleUpperCase('en-AU')||'?','cover cover-missing');node.setAttribute('aria-hidden','true');return node;}
function cover(game) {
  if(!game.artwork)return placeholder(game);
  // The title beside the image is the accessible name, so the image itself is decorative.
  const image=element('img',undefined,'cover');image.alt='';image.width=56;image.height=56;image.loading='lazy';image.decoding='async';image.src=game.artwork;
  image.addEventListener('error',()=>image.replaceWith(placeholder(game)),{once:true});
  return image;
}
function renderSummary() {
  const m=payload.metadata,s=summarise(games);
  byId('stat-hours').textContent=number(m.total_known_hours,1);
  byId('hours-note').textContent=`Known for ${number(s.hoursKnown)} of ${number(s.records)} records. The rest are unknown, so this is a floor, not a total.`;
  byId('stat-count').textContent=number(s.records);
  byId('services-note').textContent=`Across ${number(s.services)} services. Not unique games, and not a claim of ownership. ${number(s.playEvidence)} show evidence of play.`;
  byId('stat-achievements').textContent=number(s.setsComplete);
  byId('sets-note').textContent=`of ${number(s.setsKnown)} records with an achievement set (${number(s.noSet)} list none${s.achievementsUnknown?`, ${number(s.achievementsUnknown)} unknown`:''}). Not game completion.`;
  byId('last-played-note').replaceChildren(element('strong','Last played'),` is the time the source reports. ${number(s.lastPlayedKnown)} of ${number(s.records)} records have one; the rest show Unknown.`);
  byId('sync-time').textContent=m.source_retrieved_at?`Downloaded ${date(m.source_retrieved_at)}`:'Offline import · live download time unknown';
  byId('generated-time').textContent=`Report generated ${date(m.last_updated)}`;
  const stale=!m.source_retrieved_at||Date.now()-Date.parse(m.source_retrieved_at)>24*60*60*1000;
  byId('sync-indicator').className='indicator '+(stale?'stale':'ready');
  byId('freshness-note').hidden=!stale;
  byId('freshness-note').textContent=m.source_retrieved_at?'This snapshot is more than 24 hours old. The last successful download remains available.':'This is an offline snapshot. Report generation time does not establish when the source was downloaded.';
  const current=byId('service').value,counts=serviceCounts(games);
  byId('service').replaceChildren(new Option('All services',''),...counts.map(([slug,total])=>new Option(`${serviceName(slug)} (${number(total)})`,slug)));
  byId('service').value=counts.some(([slug])=>slug===current)?current:'';
}
function achievementCell(game) {
  const view=achievementView(game),cell=element('td',undefined,'number');
  if(view.state==='none'||view.state==='unknown'){cell.textContent=view.state==='none'?'No set':'Unknown';cell.classList.add('unknown');return cell;}
  cell.append(element('span',view.counts,'ach-counts'),element('span',' achievements earned','sr-only'),element('span',view.percent,'ach-percent'));
  if(view.state==='complete')cell.classList.add('achievement-complete');
  const track=element('div',undefined,'achievement-track'),fill=element('div',undefined,'achievement-fill');fill.style.width=`${view.fill}%`;track.setAttribute('aria-hidden','true');track.append(fill);cell.append(track);
  return cell;
}
function buildRow(game) {
  const row=element('tr'),title=element('td',undefined,'game-title'),holder=element('div',undefined,'game-cell'),name=element('div',undefined,'game-name');
  let url=null;try{const candidate=new URL(game.source_url);if(candidate.protocol==='https:'&&(candidate.hostname==='exophase.com'||candidate.hostname.endsWith('.exophase.com')))url=candidate.href;}catch{}
  if(url){const link=element('a',game.title);link.href=url;link.target='_blank';link.rel='noreferrer noopener';name.append(link,element('span',' (opens Exophase)','sr-only'));}else name.append(game.title);
  const played=game.last_played?dayFormat.format(new Date(game.last_played)):null;
  name.append(element('span',`${serviceName(game.service)} · Released on ${game.platforms.join(', ')||'unknown platforms'}`,'meta'),element('span',`Last played ${played||'unknown'}`,'meta meta-played'));
  holder.append(cover(game),name);title.append(holder);
  const serviceCell=element('td',undefined,'col-service');serviceCell.append(element('span',serviceName(game.service),'service-tag'));
  const platforms=element('td',game.platforms.join(', ')||'Unknown','source-platforms col-platforms');
  const playtime=element('td',game.hours===null?'Unknown':game.hours>0&&game.hours<1?`${number(game.playtime_minutes??game.hours*60)} min`:`${number(game.hours,1)} h`,'number'+(game.hours===null?' unknown':''));
  const last=element('td',played||'Unknown','number col-played'+(played?'':' unknown'));
  row.append(title,serviceCell,platforms,playtime,last,achievementCell(game));return row;
}
function renderRows() {
  if(!payload)return;
  const query=byId('search').value,service=byId('service').value,sort=byId('sort').value;
  const list=filterAndSort(games,{query,service,sort});
  byId('library-stream').replaceChildren(...list.map(buildRow));
  byId('result-count').textContent=`${number(list.length)} of ${number(games.length)} source records`;
  byId('empty').hidden=list.length>0;
  byId('clear').hidden=!(query.trim()||service);
  byId('library-table').setAttribute('aria-busy','false');
  updateScrollHint();
}
function updateScrollHint() {const scroller=document.querySelector('.table-scroll');byId('table-hint').hidden=scroller.scrollWidth<=scroller.clientWidth+1;}
function clearFilters() {byId('search').value='';byId('service').value='';renderRows();byId('search').focus();}
async function load() {
  byId('retry').disabled=true;
  try{
    const response=await fetch('output/data.json',{cache:'no-store'});
    if(!response.ok)throw new Error(`The data request failed (HTTP ${response.status}).`);
    const data=await response.json();validate(data);payload=data;games=prepare(data.games);
    renderSummary();renderRows();byId('load-error').hidden=true;
  }catch(error){
    byId('load-error').hidden=false;byId('error-text').textContent=(payload?'Showing the previously loaded snapshot. ':'')+(error instanceof SyntaxError?'The activity JSON could not be read.':error.message);
    if(!payload){byId('result-count').textContent='Data unavailable';byId('sync-time').textContent='Download time unavailable';byId('library-table').setAttribute('aria-busy','false');}
  }
  finally{byId('retry').disabled=false;}
}
byId('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(renderRows,120);});
for(const id of ['service','sort'])byId(id).addEventListener('input',renderRows);
byId('clear').addEventListener('click',clearFilters);byId('clear-empty').addEventListener('click',clearFilters);
byId('retry').addEventListener('click',load);
window.addEventListener('resize',updateScrollHint);
load();
