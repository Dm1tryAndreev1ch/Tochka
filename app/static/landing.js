const form = document.querySelector('#lead');
form.addEventListener('submit', event => {
  event.preventDefault();
  const data = new FormData(form);
  const text = `Бриф для Точки\n\nИмя: ${data.get('name').trim()}\nКонтакт: ${data.get('contact').trim()}\n\nЗадача:\n${data.get('company').trim() || 'Пока не указана'}\n`;
  const url = URL.createObjectURL(new Blob([text], {type:'text/plain;charset=utf-8'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = 'tochka-brief.txt';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  document.querySelector('#lmsg').textContent = 'Бриф сохранён на вашем устройстве. Данные не отправлены.';
});
for (const link of document.querySelectorAll('.mobile-nav a')) {
  link.addEventListener('click', () => { link.closest('details').open = false; });
}

const stages = [
  ['Сначала — ваша задача', 'Разбираемся, с чем звонят клиенты. Выбираем первый сценарий, проверяем оборудование и готовим знания компании.', 'Ориентир: 4–7 рабочих дней'],
  ['Первый полезный разговор', 'Запускаем веб-звонок: ответы на частые вопросы и подтверждённая заявка в CRM. Проверяем сценарий на ваших данных.', 'Ориентир: ещё 10–15 дней'],
  ['Помощник в рабочем ритме', 'Добавляем перебивания, запись на услуги и перевод сотруднику. Команда управляет обращениями в отдельном кабинете.', 'Ориентир: ещё 25–40 дней'],
  ['Больше точек контакта', 'После стабилизации первого сценария подключаем SIP, чаты и другие нужные каналы к единой базе знаний.', 'Состав и сроки определяем вместе'],
];
const stageButtons = [...document.querySelectorAll('[data-step]')];
for (const button of stageButtons) {
  button.addEventListener('click', () => {
    const index = Number(button.dataset.step);
    const detail = document.querySelector('#stage-detail');
    for (const item of stageButtons) item.setAttribute('aria-pressed', String(item === button));
    detail.querySelector('.stage-count').textContent = `0${index + 1} / 04`;
    detail.querySelector('h3').textContent = stages[index][0];
    detail.querySelector('p').textContent = stages[index][1];
    detail.querySelector('.stage-timing').textContent = stages[index][2];
  });
  button.addEventListener('keydown', event => {
    if (!['ArrowDown', 'ArrowUp', 'ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const current = stageButtons.indexOf(button);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? stages.length - 1
      : (current + (['ArrowDown', 'ArrowRight'].includes(event.key) ? 1 : -1) + stages.length) % stages.length;
    stageButtons[next].focus();
    stageButtons[next].click();
  });
}
