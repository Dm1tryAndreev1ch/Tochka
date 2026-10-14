import { writeFileSync, copyFileSync } from 'node:fs';
const out = new URL('./', import.meta.url);
const groups = [];
function group(name, columns) {
  groups.push(`<g id="${name}">\n${columns.flatMap(([x,...segments]) => segments.map(([y,h]) => `<rect x="${x}" y="${y}" width="16" height="${h}" rx="8"/>`)).join('\n')}\n</g>`);
}
group('wave-in', [[246,[329,18]],[274,[322,32]],[302,[314,48]],[330,[302,72]],[358,[295,86]]]);
group('letter-t', [[394,[228,28]],[420,[220,44]],[446,[212,60]],[472,[205,245]],[498,[197,264]],[524,[205,245]],[550,[212,60]],[576,[220,44]],[602,[228,28]]]);
group('letter-o', [[628,[298,68]],[654,[262,141]],[680,[234,198]],[706,[220,72],[372,73]],[732,[208,61],[396,61]],[758,[197,60],[406,62]],[784,[208,61],[396,61]],[810,[220,72],[372,73]],[836,[234,198]],[862,[262,141]],[888,[298,68]]]);
group('letter-che', [[930,[213,112]],[956,[201,150]],[982,[220,154]],[1008,[315,66]],[1034,[315,66]],[1060,[229,216]],[1086,[209,241]],[1112,[201,260]]]);
group('letter-ka', [[1178,[212,238]],[1204,[199,262]],[1230,[212,238]],[1256,[271,120]],[1282,[243,177]],[1308,[225,85],[349,92]],[1334,[209,66],[384,73]],[1360,[202,38],[416,52]]]);
group('letter-a', [[1412,[411,40]],[1438,[349,112]],[1464,[292,158]],[1490,[247,176]],[1516,[213,113],[351,52]],[1542,[197,96],[351,52]],[1568,[213,113],[351,52]],[1594,[247,176]],[1620,[292,158]],[1646,[349,112]],[1672,[411,40]]]);
group('wave-out', [[1684,[298,83]],[1712,[304,71]],[1740,[315,49]],[1768,[326,30]],[1796,[332,18]]]);
const bars = groups.join('\n');
for (const [name,fg,bg] of [
 ['tochka-waveform-inverse','#f2f0f4','#2f2838'],
 ['tochka-waveform-light','#2f2838','#f2f0f4'],
 ['tochka-waveform-transparent','#2f2838',null],
 ['tochka-waveform-white-transparent','#f2f0f4',null]
]) {
 const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="180 115 1708 436" role="img" aria-labelledby="title desc">\n<title id="title">Точка</title>\n<desc id="desc">Слово ТОЧКА из вертикальных полос звуковой волны.</desc>\n${bg ? `<path fill="${bg}" d="M180 115h1708v436H180z"/>` : ''}\n<g fill="${fg}">\n${bars}\n</g>\n</svg>\n`;
 writeFileSync(new URL(`${name}.svg`,out),svg);
}
copyFileSync('/Users/andreevich/.codex/generated_images/01a134dd-ef7a-7190-824c-e3a94b157e37/exec-11eeaa01-a6bf-4774-a2ec-0b2c83548a8f.png',new URL('tochka-waveform-concept.png',out));
console.log('Created four native SVG variants and saved the generated concept.');
