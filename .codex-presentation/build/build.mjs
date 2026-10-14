import fs from 'node:fs/promises';
import { Presentation, PresentationFile } from '@oai/artifact-tool';
const root='/Users/andreevich/Documents/Projects/codex/voice';
const out=root+'/.codex-presentation/build';
const p=Presentation.create({slideSize:{width:1280,height:720}});
p.theme.defaultFont='Golos Text';
const C={bg:'#f2f0f4',ink:'#17121d',dark:'#221d29',aub:'#2f2838',ash:'#b9b4c2',dim:'#656170',line:'#d9d5de',white:'#ffffff'};
const buf=async path=>{let b=await fs.readFile(path);return b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength)};
async function img(s,path,x,y,w,h,name){return s.images.add({blob:await buf(path),contentType:path.endsWith('svg')?'image/svg+xml':'image/png',alt:name,fit:'contain',name,position:{left:x,top:y,width:w,height:h}})}
function tx(s,text,x,y,w,h,size=28,color=C.ink,name='body',align='left'){
 const q=s.shapes.add({geometry:'textbox',name,position:{left:x,top:y,width:w,height:h},fill:'none',line:{fill:'none',width:0}});q.text=text;q.text.style={typeface:'Golos Text',fontSize:size,color,alignment:align,verticalAlignment:'top',insets:0,autoFit:'none',wrap:'none'};return q;
}
function box(s,text,x,y,w,h,dark=false,name='diagram'){
 const q=s.shapes.add({geometry:'roundRect',name,position:{left:x,top:y,width:w,height:h},borderRadius:6,fill:dark?C.aub:C.white,line:{fill:dark?C.ash:C.line,width:1}});q.text=text;q.text.style={typeface:'Golos Text',fontSize:25,color:dark?C.bg:C.ink,alignment:'center',verticalAlignment:'middle',insets:15,autoFit:'none'};return q;
}
function connect(s,a,b,dark=false,sideA='right',sideB='left'){return s.shapes.connect(a,b,{kind:'elbow',fromSide:sideA,toSide:sideB,line:{fill:dark?C.ash:C.dim,width:2},tail:{type:'triangle',width:'sm',length:'sm'}})}
async function slide(title,dark=false,note=''){
 let s=p.slides.add();s.background.fill=dark?C.dark:C.bg;
 await img(s,root+'/app/static/brand/tochka-'+(dark?'light':'dark')+'.svg',64,39,190,33.17,'static-logo');
 tx(s,String(p.slides.items.length).padStart(2,'0'),1160,46,55,28,18,dark?C.ash:C.dim,'static-page','right');
 if(title)tx(s,title,64,125,1152,124,50,dark?C.bg:C.ink,'title');
 s.speakerNotes.text=note+'\n\nИсточник содержания: «Точка.pptx», предоставленная презентация. Дизайн: docs/design/DESIGN_SYSTEM.md, локальный Golos Text, утверждённые SVG «Точки».';
 return s;
}
let s=await slide('',true,'Точка — концепция ИИ-агента для входящих звонков. Презентация адресована потенциальному партнёру-оператору. Интеграция и экономика далее представлены как проектные предложения, а не как заключённые договорённости.');
// Approved logo is a static hero, with no alteration.
s.images.items[0].position={left:64,top:74,width:550,height:96.02};
tx(s,'ИИ-агент для\nвходящих звонков',64,286,1120,190,76,C.bg,'hero');
tx(s,'Принимает обращение, пока вы заняты',68,550,1100,55,31,C.ash,'reveal-1');

s=await slide('Клиент звонит,\nкогда вы заняты',false,'Персона исходной презентации: Иван Ракевич, 23 года, владелец небольшой IT-компании. Это образ целевого пользователя, не подтверждённый кейс клиента. Его день занят поездками и встречами. Пропущенный неизвестный номер может оказаться новым клиентом или партнёром. Точная сумма потерь не оценивалась. Иллюстрация создана ИИ, персонаж вымышленный.');
await img(s,out+'/client.png',777,0,503,720,'Иллюстрация занятого предпринимателя');
// Place photo only in right field, title remains on the left.
tx(s,'Владельцы небольших компаний\nи руководители IT-бизнеса',64,330,660,90,29,C.dim,'reveal-1');
tx(s,'Пропущенный звонок —\nриск потерять обращение',64,504,665,110,36,C.ink,'reveal-2');

s=await slide('Как Точка обрабатывает звонок',false,'Целевой сценарий: агент принимает входящий звонок, выясняет запрос и сохраняет результат разговора. Если вопрос требует вмешательства сотрудника, нужен предусмотренный сценарий передачи человеку. Наличие конкретных действий зависит от подключённых каналов и интеграций.');
const labels=[['01','Принимает','Входящий звонок'],['02','Уточняет','Запрос клиента'],['03','Сохраняет','Итог разговора'],['04','Передаёт','Человеку при\nнеобходимости']];
let prev;
labels.forEach((a,i)=>{let x=64+i*294;tx(s,a[0],x,296,240,82,64,C.dim,'reveal-'+(i+1));let b=box(s,a[1],x,407,248,74,false,'reveal-'+(i+1));tx(s,a[2],x,507,257,80,i===3?23:25,C.dim,'reveal-'+(i+1));if(prev)connect(s,prev,b);prev=b;});

s=await slide('Две модели для разной сложности',true,'Проектная каскадная архитектура исходника: STT переводит речь в текст, лёгкая LLM обрабатывает типовые запросы, тяжёлая модель используется для сложных задач, TTS синтезирует ответ. Цель — расходовать меньше вычислительных ресурсов на типовые диалоги. Экономию и качество необходимо измерить на пилоте. В перспективе: аналитика частых вопросов, улучшение сценариев на основе реальных диалогов и история обращений.');
let a=box(s,'Речь\nклиента',64,320,170,110,true,'reveal-1');let b=box(s,'STT\nРечь в текст',278,320,205,110,true,'reveal-1');let c=box(s,'Лёгкая LLM\nТиповые вопросы',535,262,315,112,true,'reveal-2');let d=box(s,'Тяжёлая LLM\nСложные запросы',535,463,315,112,true,'reveal-3');let e=box(s,'TTS\nГолосовой ответ',943,320,273,110,true,'reveal-4');
connect(s,a,b,true);connect(s,b,c,true);connect(s,c,e,true);connect(s,c,d,true,'bottom','top');connect(s,d,e,true);tx(s,'При сложном\nзапросе',708,393,210,60,18,C.ash,'reveal-3');
tx(s,'Простые диалоги не требуют тяжёлой модели',64,624,1152,45,29,C.ash,'reveal-4');

s=await slide('Доверие на каждом этапе',false,'Карта пути клиента из исходника, укрупнённая до трёх периодов. До подключения: профессиональное впечатление, кейсы экономии времени, пробный период и демонстрация аналитики. На старте: быстрый мастер настройки, понятная работа с данными, примеры диалогов, ручная коррекция. При регулярной работе: сохранение сведений о клиенте, перевод на сотрудника, CRM/email-модули и консультации по расширению. Это проектные принципы клиентского опыта; условия пробного периода и интеграций требуют определения.');
[['До подключения','Демо и пробный период','Помогают оценить пользу'],['Первый звонок','Примеры и коррекция','Помогают проверить точность'],['Ежедневная работа','История и перевод человеку','Помогают сохранить контроль']].forEach((v,i)=>{let x=64+398*i;tx(s,'0'+(i+1),x,282,340,83,67,C.dim,'reveal-'+(i+1));tx(s,v[0],x,401,350,55,31,C.ink,'reveal-'+(i+1));tx(s,v[1],x,486,360,68,25,C.ink,'reveal-'+(i+1));tx(s,v[2],x,568,350,76,23,C.dim,'reveal-'+(i+1));});

s=await slide('Подключение в «Мой МТС»',true,'Предлагаемый сценарий партнёрской интеграции из исходной презентации. Пользователь активирует услугу в приложении оператора, даёт необходимые согласия, выбирает правила обработки звонков и проверяет агента. Хостинг внутри инфраструктуры МТС, работа на существующем номере и списание с баланса требуют технического и коммерческого согласования. Юридические условия обработки данных необходимо уточнять отдельно.');
tx(s,'Предлагаемый сценарий интеграции',64,220,1120,42,25,C.ash,'static-disclosure');
[['01','Включить услугу'],['02','Задать правила'],['03','Проверить звонком']].forEach((v,i)=>{let x=64+i*398;tx(s,v[0],x,340,340,115,88,C.ash,'reveal-'+(i+1));tx(s,v[1],x,497,360,65,32,C.bg,'reveal-'+(i+1));});
tx(s,'Без отдельного хостинга и API-ключей у клиента',64,625,1150,42,25,C.ash,'reveal-4');

s=await slide('Модель подписки',false,'Предварительная коммерческая гипотеза из исходника. Тариф 9,90 BYN в месяц. Предлагаемое распределение выручки 50/50: 4,95 BYN оператору и 4,95 BYN команде. При 1 000 платных абонентов общая месячная выручка — 9 900 BYN, доля каждой стороны — 4 950 BYN до расходов и налогов. Это не прибыль. Лимиты минут, оплата превышения, плательщик вычислительных и телефонных расходов и условия партнёрства ещё не определены. Утверждения исходника о нулевой себестоимости и окупаемости с одного абонента не подтверждаются его же биллинговой таблицей.');
tx(s,'9,90',64,255,570,178,146,C.ink,'reveal-1');tx(s,'BYN в месяц',73,435,550,60,34,C.dim,'reveal-1');
tx(s,'50 / 50',760,275,440,88,65,C.ink,'reveal-2');tx(s,'по 4,95 BYN\nоператору и команде',763,388,448,94,29,C.dim,'reveal-2');
tx(s,'1 000 подписчиков',64,579,388,43,29,C.ink,'reveal-3');tx(s,'4 950 BYN / мес. команде до расходов',460,579,755,48,29,C.ink,'reveal-3');
tx(s,'Предварительная модель. Лимиты и условия партнёрства требуют согласования.',64,662,1152,30,19,C.dim,'static-disclosure');

s=await slide('Себестоимость зависит от нагрузки',false,'Ставки — расчётные допущения из исходной презентации, не подтверждённые тарифы поставщиков. Телефония — 0,05 BYN/мин, STT/TTS — 0,05 BYN/мин. Лёгкая LLM — 0,02 BYN за 1 000 токенов, тяжёлая — 0,08 BYN за 1 000 токенов. Формула базовой модели: 0,10 × минуты + 0,02 × тысячи токенов лёгкой модели + 0,08 × тысячи токенов тяжёлой модели. При каскадной обработке учитывать токены обеих моделей. Дополнительно оценить инфраструктуру, разработку, поддержку, налоги и другие постоянные расходы. Указанные в исходнике 0–350 BYN на комплаенс и SLA 99,9%+ не имеют расчёта или подтверждения и не используются как обещания.');
const vals=[['Компонент','Расчётная ставка','Единица'],['Телефония','0,05 BYN','1 минута'],['Распознавание и синтез речи','0,05 BYN','1 минута'],['Лёгкая LLM','0,02 BYN','1 000 токенов'],['Тяжёлая LLM','0,08 BYN','1 000 токенов']];
let tab=s.tables.add({rows:5,columns:3,left:64,top:251,width:1152,height:308,columnWidths:[568,292,292],values:vals});tab.name='billing-table';
tab.borders.assign({fill:C.line,width:1});
for(let r=0;r<5;r++){tab.rows[r].height=r===0?52:64;for(let c=0;c<3;c++){let cell=tab.getCell(r,c);cell.fill=r===0?C.dark:C.bg;cell.text.style={typeface:'Golos Text',fontSize:r===0?22:25,color:r===0?C.bg:C.ink,insets:14,verticalAlignment:'middle'};}}
tx(s,'0,10 BYN × минуты + расходы на LLM',64,594,1152,52,34,C.ink,'reveal-1');tx(s,'Расчётные ставки. Инфраструктура и поддержка учитываются дополнительно.',64,662,1152,30,19,C.dim,'static-disclosure');

s=await slide('Сценарий демонстрации',false,'Предлагаемый сценарий живой демонстрации, а не запись реального разговора. Позвонить агенту, задать вопрос о времени работы, затем попросить сотрудника перезвонить. Проверить, что агент уточнил контактные данные и запрос, а в кабинете появился фактический результат. Показывать только реально подключённые и работающие действия. Передачу человеку демонстрировать только при готовой телефонии.');
tx(s,'«Здравствуйте. Вы сегодня\nработаете? Хочу обсудить\nподключение»',64,278,730,210,46,C.ink,'reveal-1');
tx(s,'Что проверяем',876,280,340,48,28,C.dim,'reveal-2');tx(s,'Ответ по знаниям\n\nУточнение запроса\n\nИтог в кабинете',876,359,340,240,27,C.ink,'reveal-3');
tx(s,'Один звонок: от вопроса клиента до сохранённого обращения',64,628,1152,52,28,C.dim,'reveal-4');

s=await slide('Следующий шаг — пилот',true,'Предложение по следующему этапу: согласовать сценарии, ограниченную группу абонентов, правила передачи человеку, обработку данных, бюджет и целевые метрики. До масштабирования измерить долю завершённых обращений, задержку ответа, качество распознавания и фактическую стоимость минуты. Команда в исходном файле не перечислена, поэтому состав и имена не заявляются. Дальнейшее развитие из исходника: CRM/email, аналитика частых вопросов и улучшение сценариев по результатам диалогов.');
tx(s,'Проверить на реальных звонках',64,255,1120,60,36,C.ash,'reveal-1');
[['Качество','Завершённые обращения'],['Скорость','Задержка ответа'],['Экономика','Стоимость минуты']].forEach((v,i)=>{let x=64+i*398;tx(s,v[0],x,407,360,66,38,C.bg,'reveal-'+(i+2));tx(s,v[1],x,495,360,70,26,C.ash,'reveal-'+(i+2));});
tx(s,'Согласовать сценарии, группу абонентов и критерии успеха',64,635,1152,50,28,C.bg,'reveal-5');

await fs.mkdir(out+'/render',{recursive:true});
for(let i=0;i<p.slides.items.length;i++){const png=await p.export({slide:p.slides.items[i],format:'png',scale:1.25});await fs.writeFile(out+`/render/${i+1}.png`,new Uint8Array(await png.arrayBuffer()));}
await (await PresentationFile.exportPptx(p)).save(out+'/draft.pptx');
await fs.writeFile(out+'/inspect.ndjson',(await p.inspect({kind:'slide,textbox,table',maxChars:1000000})).ndjson);
console.log('Rendered and exported',p.slides.items.length,'slides');
