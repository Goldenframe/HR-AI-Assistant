import fs from 'node:fs/promises';
import {Workbook, SpreadsheetFile, FileBlob} from '@oai/artifact-tool';

const root = new URL('../', import.meta.url);
const questions = JSON.parse(await fs.readFile(new URL('data/export-questions.json', root), 'utf8'));
const wb = Workbook.create();
const sheet = wb.worksheets.add('Ответы');
const headers = ['На кого обратная связь', 'Отзыв', ...questions.map(q => q.text)];
sheet.getRangeByIndexes(0, 0, 1, headers.length).values = [headers];
sheet.getRangeByIndexes(1, 0, 1, headers.length).values = [['Сотрудник', 1, ...questions.map(q => q.type === 'rating' ? 'Скорее согласен' : 'Пример комментария.')]];
const area = sheet.getRangeByIndexes(0, 0, 2, headers.length);
area.format.font = {name:'Arial', size:10, color:'#172033'};
area.format.wrapText = true;
area.format.verticalAlignment = 'top';
area.format.columnWidth = 28;
sheet.getRangeByIndexes(0, 0, 1, headers.length).format = {
  fill:'#254469', font:{name:'Arial',size:10,bold:true,color:'#FFFFFF'},
  wrapText:true, horizontalAlignment:'center', verticalAlignment:'center', rowHeight:100,
};
sheet.getRangeByIndexes(1, 0, 1, headers.length).format.rowHeight = 42;
sheet.getRange('A1:A2').format.columnWidth = 26;
sheet.getRange('B1:B2').format.columnWidth = 10;
sheet.getRange('B2').format.horizontalAlignment = 'center';
sheet.getRangeByIndexes(0,0,1,headers.length).format.borders = {preset:'inside',style:'thin',color:'#FFFFFF'};
sheet.getRangeByIndexes(1,0,1,headers.length).format.borders = {insideVertical:{style:'thin',color:'#D8DFE8'}};
for(let i=0;i<questions.length;i++) {
  if(questions[i].type==='comment') {
    sheet.getRangeByIndexes(0,i+2,2,1).format.columnWidth=45;
    sheet.getRangeByIndexes(1,i+2,1,1).format.fill='#F0F4F8';
  }
}
sheet.showGridLines=false;
sheet.freezePanes.freezeRows(1);
sheet.freezePanes.freezeColumns(2);
wb.recalculate();
console.log((await wb.inspect({kind:'region',sheetId:'Ответы',range:'A1:E2',maxChars:800,tableMaxCellChars:45})).ndjson);
const xlsx=await SpreadsheetFile.exportXlsx(wb);
await xlsx.save(new URL('fixtures/feedback-export-template.xlsx',root).pathname.replace(/^\/([A-Z]:)/,'$1'));
// Preview either the real filled export or the authored template.
let previewWorkbook=wb;
try {
  previewWorkbook=await SpreadsheetFile.importXlsx(await FileBlob.load(new URL('data/feedback-answers.xlsx',root).pathname.replace(/^\/([A-Z]:)/,'$1')));
} catch {}
const png=await previewWorkbook.render({sheetName:'Ответы',range:'A1:F3',scale:1.5,format:'png'});
await fs.writeFile(new URL('data/feedback-preview.png',root),new Uint8Array(await png.arrayBuffer()));
