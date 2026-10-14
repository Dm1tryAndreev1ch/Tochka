import {writeFileSync,copyFileSync} from 'node:fs';
const groups=[
 ['t',[[417,218,55],[449,218,55],[481,218,231],[513,218,231],[545,218,55],[577,218,55]]],
 ['o',[[660,263,144],[692,234,204],[724,213,73],[724,387,71],[756,201,67],[756,405,65],[788,213,73],[788,387,71],[820,234,204],[852,263,144]]],
 ['che',[[930,213,112],[962,213,138],[994,298,71],[1026,313,56],[1058,213,244],[1090,213,244]]],
 ['ka',[[1194,213,244],[1226,213,244],[1258,274,119],[1290,251,73],[1290,339,78],[1322,232,68],[1322,365,74],[1354,213,61],[1354,396,61]]],
 ['a',[[1428,288,169],[1460,251,206],[1492,220,84],[1492,339,54],[1524,202,84],[1524,339,54],[1556,220,84],[1556,339,54],[1588,251,206],[1620,288,169]]]
];
const bars=groups.flatMap(g=>g[1]);
for(let i=0;i<bars.length;i++) for(let j=i+1;j<bars.length;j++){
 const [ax,ay,ah]=bars[i],[bx,by,bh]=bars[j];
 if(ax<bx+20&&ax+20>bx&&ay<by+bh&&ay+ah>by)throw Error('Overlapping bars');
}
for(const [name,fg,bg] of [['inverse','#f2f0f4','#2f2838'],['transparent','#2f2838',null],['white-transparent','#f2f0f4',null]]){
 const svg=`<svg xmlns="http://www.w3.org/2000/svg" viewBox="320 130 1416 410" role="img" aria-labelledby="title"><title id="title">Точка</title>${bg?`<path fill="${bg}" d="M320 130h1416v410H320z"/>`:''}<g fill="${fg}">${groups.map(([id,rs])=>`<g id="letter-${id}">${rs.map(([x,y,h])=>`<rect x="${x}" y="${y}" width="20" height="${h}" rx="10"/>`).join('')}</g>`).join('')}</g></svg>\n`;
 writeFileSync(`output/logo/tochka-minimal-${name}.svg`,svg);
}
copyFileSync('/Users/andreevich/.codex/generated_images/01a134dd-ef7a-7190-824c-e3a94b157e37/exec-64a6b3d6-1e67-4fea-add8-63f0dc5fb9ad.png','output/logo/tochka-minimal-concept.png');
console.log(`PASS: 5 letters; ${bars.length} rounded bars; no overlapping bars; three native SVG files saved.`);
