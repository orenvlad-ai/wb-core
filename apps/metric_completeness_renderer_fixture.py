"""Build a local synthetic preview using the production cell formatter verbatim."""
import json
from pathlib import Path
import re
import sys
ROOT=Path(__file__).resolve().parents[1]

def functions():
    template=(ROOT/'packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html').read_text()
    names=['completenessMarker','formatCellContent','formatBaseCellContent','formatCellValue','formatCellTitle','toComparableNumber','formatNumber','escapeHtml']
    result=[]
    for name in names:
        start=template.index('    function '+name+'(')
        next_function=re.search(r'^    function ',template[start+5:],re.M)
        assert next_function,name
        result.append(template[start:start+5+next_function.start()])
    return '\n'.join(result)

def fixture():
    cases=[('Полный итог','total',1200,'complete',0),('Частичный итог','total',1200,'partial',2),('Группа: один SKU','group',450,'partial',1),('Неизвестный состав','total',60828.56,'unknown_scope',None),('Все отсутствуют','total',None,'partial',3),('Подтверждённый ноль','total',0,'complete',0),('Наблюдаемый ноль','group',0,'unknown_scope',None)]
    rows=[{'label':label,'row_kind':kind,'cell':{'value':value,'cell_kind':'money','formatter_id':'money','completeness_state':state,'missing_sku_count':count,'quality_reason':'Синтетический пример для независимой проверки. Причина доступна в tooltip и описании.'}} for label,kind,value,state,count in cases]
    template=(ROOT/'packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html').read_text()
    production_style=template[template.index('<style>'):template.index('</style>')+8]
    return production_style+'''<!doctype html><meta charset="utf-8"><title>Ads — проверка частичных итогов</title><style>body{background:#15171c;color:#e9ebef;font:16px system-ui;padding:36px}table{border-collapse:collapse;min-width:720px}td,th{padding:18px 24px;border-bottom:1px solid #363b44;text-align:right}td:first-child,th:first-child{text-align:left}p{color:#afb8c8}td.money{color:#dce4ef;font-variant-numeric:tabular-nums}</style><h1>Частичные итоги</h1><p>Контролируемые примеры. Форматирование ячеек взято из рабочего renderer.</p><table><thead><tr><th>Состояние</th><th>11 сентября</th></tr></thead><tbody id="rows"></tbody></table><h2>Плотность рабочей таблицы</h2><div class="table-shell" style="width:528px;margin-top:18px"><div class="table-scroll"><table class="vitrina-table"><thead><tr><th style="width:264px">Метрика</th><th style="width:88px">10 сентября</th><th style="width:88px">11 сентября</th><th style="width:88px">12 сентября</th></tr></thead><tbody id="dense"></tbody></table></div></div><script>
const formattersById=new Map([['money',{decimals:2,thousands_separator:true,null_display:'—'}]]);const renderersById=new Map();
'''+functions()+'''\nconst examples='''+json.dumps(rows,ensure_ascii=False)+''';
const column={id:'date:2026-09-11'};
document.getElementById('rows').innerHTML=examples.map(row=>'<tr><td>'+escapeHtml(row.label)+'</td><td class="money" title="'+escapeHtml(formatCellTitle(column,row,row.cell))+'" aria-label="'+escapeHtml(formatCellTitle(column,row,row.cell))+'">'+formatCellContent(column,row,row.cell)+'</td></tr>').join('');
document.getElementById('dense').innerHTML=examples.map(row=>'<tr class="metric-data-row"><td data-col-id="metric_label">'+escapeHtml(row.label)+'</td>'+[0,1,2].map((_,i)=>{const c=i===1?row.cell:{...row.cell,value:i===0?1200.45:1700,completeness_state:'complete',missing_sku_count:0};return '<td style="width:88px;min-width:88px;max-width:88px;text-align:right" title="'+escapeHtml(formatCellTitle(column,row,c))+'" aria-label="'+escapeHtml(formatCellTitle(column,row,c))+'">'+formatCellContent(column,row,c)+'</td>';}).join('')+'</tr>').join('');
</script>'''
if __name__=='__main__':
    path=Path(sys.argv[1]);path.write_text(fixture());print(path)
