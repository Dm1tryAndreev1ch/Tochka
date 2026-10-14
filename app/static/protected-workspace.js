import {$,$$,esc,toast,dateLabel} from './shared.js';

const status={pending:'Ожидает',synced:'Доставлено'};
const number=v=>Number(v||0).toLocaleString('ru-RU');
const reasons={service_support:'Обработка обращения',quality_review:'Проверка качества',customer_request:'Запрос клиента',security_review:'Проверка безопасности'};
const recordingLabels={not_recorded:'Не записывалось',incomplete:'Запись не завершена',unavailable:'Ошибка записи',expired:'Срок хранения истёк',deleted:'Удалено'};
const recordingLabel=r=>r.audio_available?number(r.audio_seconds)+' с':recordingLabels[r.audio_status]||'Не записывалось';
export function protectedViews({api,content,title,can}){
 let audioUrl=null;
 const purposeSelect=()=>'<label>Цель доступа<select id="archive-purpose"><option value="">Выберите цель</option>'+Object.entries(reasons).map(([v,l])=>'<option value="'+v+'">'+l+'</option>').join('')+'</select></label>';
 function chart(rows,key,label){
  if(!rows.length)return '<div class="empty">Данных за период пока нет.</div>';
  const values=rows.map(r=>Number(r[key]||0)),max=Math.max(1,...values),width=Math.max(420,rows.length*48);
  return '<div class="data-chart"><svg viewBox="0 0 '+width+' 160" role="img" aria-label="'+esc(label)+'">'+rows.map((r,i)=>{const height=values[i]/max*105;return '<g><rect class="data-chart-bar" x="'+(i*48+12)+'" y="'+(120-height)+'" width="26" height="'+Math.max(1,height)+'" rx="3"/><text x="'+(i*48+25)+'" y="'+(110-height)+'" text-anchor="middle">'+values[i]+'</text><text x="'+(i*48+25)+'" y="145" text-anchor="middle">'+esc(String(r.day).slice(5))+'</text></g>'}).join('')+'</svg></div>';
 }
 async function crm(){
  const data=await api('/operator/crm-summary');
  content.innerHTML=title()+'<div class="metric-grid">'+data.counts.map(r=>'<div class="metric-card"><span>'+esc(status[r.status]||r.status)+'</span><strong>'+number(r.count)+'</strong><small>События CRM</small></div>').join('')+'</div><div class="panel"><div class="panel-header"><h2>Результаты по дням</h2></div>'+chart(data.daily.filter(r=>r.status==='synced'),'count','Доставленные события CRM по дням')+'</div><div class="panel"><div class="panel-header"><h2>Последние отправления</h2><span class="tiny">Без имён и телефонов</span></div>'+(data.recent.length?'<div class="table-wrap"><table><thead><tr><th>Время</th><th>Обращение</th><th>Сущность</th><th>Результат</th><th>Попытки</th><th></th></tr></thead><tbody>'+data.recent.map(r=>'<tr><td>'+dateLabel(r.last_attempt_at||r.created_at)+'</td><td>'+esc(r.kind==='booking'?'Запись':r.kind==='callback'?'Обратный звонок':'Обращение')+'<span class="table-sub">'+esc(r.action_id.slice(0,8))+'</span></td><td>'+esc(r.entity)+'</td><td><span class="badge '+(r.status==='synced'?'done':'pending')+'">'+esc(status[r.status]||r.status)+'</span><span class="table-sub">'+esc(r.error||'')+'</span></td><td>'+r.attempts+'</td><td>'+(r.status==='pending'&&can('crm.retry')?'<button class="small-button" data-crm-retry="'+r.id+'">Повторить</button>':'')+'</td></tr>').join('')+'</tbody></table></div>':'<div class="empty">Отправлений пока нет.</div>')+'</div>';
  for(const button of $$('[data-crm-retry]'))button.onclick=async()=>{button.disabled=true;try{await api('/operator/outbox/'+button.dataset.crmRetry+'/retries',{method:'POST'});toast('Повтор запланирован');await crm()}catch(e){toast(e.message);button.disabled=false}};
 }
 async function archive(){
  if(audioUrl){URL.revokeObjectURL(audioUrl);audioUrl=null}
  const data=await api('/operator/archive');
  content.innerHTML=title()+'<div class="panel"><div class="panel-header"><h2>Завершённые разговоры</h2><span class="tiny">Защищённое хранение</span></div><div class="system-body">'+purposeSelect()+'<p class="form-help">Каждый просмотр, воспроизведение и удаление фиксируется с вашей учётной записью и целью доступа.</p></div>'+(data.items.length?'<div class="table-wrap"><table><thead><tr><th>Разговор</th><th>Канал</th><th>Аудио</th><th>Хранение текста</th><th>Доступ</th></tr></thead><tbody>'+data.items.map(r=>'<tr><td>'+dateLabel(r.ended_at)+'<span class="table-sub">'+r.id.slice(0,8)+'</span></td><td>'+esc(r.source)+'</td><td>'+ recordingLabel(r)+'</td><td>'+dateLabel(r.transcript_expires_at)+'</td><td><div class="actions">'+(can('archive.text')&&r.transcript_available!==false?'<button class="small-button" data-text="'+r.id+'">Текст</button>':'')+(can('archive.audio')&&r.audio_available?'<button class="small-button" data-audio="'+r.id+'">Слушать</button>':'')+(can('archive.export')&&can('archive.audio')&&r.audio_available?'<button class="small-button" data-export="'+r.id+'">Скачать</button>':'')+(can('privacy.delete')?'<button class="small-button danger-text" data-erase="'+r.id+'">Удалить</button>':'')+'</div></td></tr>').join('')+'</tbody></table></div>':'<div class="empty">Архив появится после завершения первого разговора. Аудио сохраняется только при включённой политике и устном согласии.</div>')+'</div><div class="panel" id="archive-detail" hidden></div>';
  const purpose=()=>{const value=$('#archive-purpose').value;if(!value)throw new Error('Выберите цель доступа к записи');return value};
  for(const button of $$('[data-text]'))button.onclick=async()=>{try{const result=await api('/operator/archive/'+button.dataset.text+'/transcript?purpose='+purpose());const detail=$('#archive-detail');detail.hidden=false;detail.innerHTML='<div class="panel-header"><h2>Расшифровка</h2></div><div class="system-body">'+result.turns.map(t=>'<div class="archive-turn"><span class="tiny">'+esc({user:'Клиент',assistant:'Агент',operator:'Сотрудник'}[t.role]||t.role)+'</span><p>'+esc(t.generated_text||t.text)+'</p>'+(t.role==='assistant'&&t.delivery!=='delivered'?'<small>Воспроизведение полного ответа не подтверждено</small>':'')+'</div>').join('')+'</div>'}catch(e){toast(e.message)}};
  async function readAudio(id,download){
   const why=purpose();const response=await fetch('/api/v1/operator/archive/'+id+'/audio?purpose='+why+(download?'&download=true':''),{credentials:'same-origin'});
   if(!response.ok){const result=await response.json();throw new Error(result.error?.message||'Запись недоступна')}
   const blob=await response.blob();if(audioUrl)URL.revokeObjectURL(audioUrl);audioUrl=URL.createObjectURL(blob);
   if(download){const link=document.createElement('a');link.href=audioUrl;link.download='conversation-'+id+'.wav';link.click()}
   else{const detail=$('#archive-detail');detail.hidden=false;detail.innerHTML='<div class="panel-header"><h2>Аудиозапись</h2></div><div class="system-body"><audio id="archive-player" controls></audio><p class="form-help">Клиент и собеседник записаны в отдельных каналах.</p></div>';$('#archive-player').src=audioUrl;await $('#archive-player').play()}
  }
  for(const button of $$('[data-audio]'))button.onclick=()=>readAudio(button.dataset.audio,false).catch(e=>toast(e.message));
  for(const button of $$('[data-export]'))button.onclick=()=>readAudio(button.dataset.export,true).catch(e=>toast(e.message));
  for(const button of $$('[data-erase]'))button.onclick=async()=>{try{const why=purpose();if(!confirm('Удалить аудио и расшифровку этого разговора? Действие фиксируется в аудите.'))return;await api('/operator/archive/'+button.dataset.erase+'?purpose='+why,{method:'DELETE'});await archive();toast('Доступ отозван, удаление из хранилища запланировано')}catch(e){toast(e.message)}};
 }
 async function audit(){
  const data=await api('/operator/audit');
  content.innerHTML=title()+'<div class="panel"><div class="panel-header"><h2>Последние действия и доступы</h2></div><div class="table-wrap"><table><thead><tr><th>Время</th><th>Учётная запись</th><th>Действие</th><th>Объект</th><th>Цель</th><th>Результат</th></tr></thead><tbody>'+data.items.map(r=>'<tr><td>'+dateLabel(r.created_at)+'</td><td>'+esc(r.actor)+'</td><td>'+esc(r.event)+'</td><td>'+esc(r.resource_id||'—')+'</td><td>'+esc(reasons[r.purpose]||r.purpose||'Системное действие')+'</td><td>'+esc(r.outcome==='denied'?'Отказ':'Разрешено')+'</td></tr>').join('')+'</tbody></table></div><p class="panel-note">Журнал не содержит текста разговоров, паролей и ключей. Старые записи без индивидуального пользователя относятся к прежней версии.</p></div>';
 }
 return {archive,crm,audit};
}
