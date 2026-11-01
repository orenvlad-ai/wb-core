"""Bounded, source-conserving 724/725 document import through the safe launcher.

No payments or receipts are created. Original attachments are copied unchanged.
A partial submit is deliberately ambiguous and requires recovery, never retry.
"""
from contextlib import closing
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import socket

from apps.production_apply_contract import AmbiguousSubmit
from packages.application.supplier_shipment_invoice_revision import connect, digest, encoded, require, rows
from packages.application.storage_registry import StoreRegistry
from packages.application.supplier_shared_document_allocation import allocate_shared_document

IDS = {'26GN724': 'sup_2822298fed2543a4b9c36fa0ccbc9bcc', '26GN725': 'sup_345043dcc1d545e98ad3f6722bc6f2ee'}
PREFIX = 'sheet_vitrina_v1_'


def capture(conn):
    result = {}
    for name, identity in IDS.items():
        tables = {}
        for table, column in [('supplier_shipments','shipment_id'), ('supplier_shipment_lines','shipment_id'), ('supplier_financial_documents','supplier_order_id'), ('supplier_financial_expense_lines','supplier_order_id'), ('cny_documents','source_order_id'), ('cny_ledger_operations','source_order_id'), ('supplier_payment_fee_confirmations','supplier_order_id'), ('supplier_bank_operation_assignments','supplier_order_id'), ('supplier_preparation_intents','shipment_id')]:
            where, values = (column+'=? OR context_order_id=?', (identity,identity)) if table == 'cny_documents' else (column+'=?', (identity,))
            tables[table] = rows(conn, PREFIX+table, where, values)
        require(len(tables['supplier_shipments']) == 1 and tables['supplier_shipments'][0]['invoice_no'] == name, 'shared-owner-identity-mismatch')
        result[name] = tables
    return result


def cost_proof(before, after):
    from packages.application.warehouse_functional import _supplier_cost_allocations
    names={'shipments':'supplier_shipments','shipment_lines':'supplier_shipment_lines','financial_documents':'supplier_financial_documents','financial_expense_lines':'supplier_financial_expense_lines','cny_documents':'cny_documents','cny_operations':'cny_ledger_operations','bank_operation_assignments':'supplier_bank_operation_assignments','payment_fee_confirmations':'supplier_payment_fee_confirmations'}
    sources={key:[deepcopy(row) for tables in before.values() for row in tables[table]] for key,table in names.items()}
    for item in after:
        document=deepcopy(item['document'])
        for field in ('normalized_parse','raw_parse','warnings','errors'):
            document[field+'_json']=json.dumps(document.pop(field),ensure_ascii=False)
        sources['financial_documents'].append(document)
        for line in item['expense_lines']:
            row=deepcopy(line);row['raw_json']=json.dumps(row.pop('raw'));sources['financial_expense_lines'].append(row)
    proof=_supplier_cost_allocations(sources)
    for identity in IDS.values():
        require(identity in proof and not proof[identity]['blockers'], 'shared-canonical-cost-blocked')
        require(all(c['conserved'] for c in proof[identity]['component_controls']), 'shared-canonical-cost-not-conserved')
        controls={c['source_component_id']:c for c in proof[identity]['component_controls']}
        for item in after:
            if item['document']['supplier_order_id'] != identity:continue
            for line in item['expense_lines']:
                control=controls.get('expense_line:'+line['line_id'])
                require(control is not None and Decimal(str(control['source_amount_rub']))==Decimal(str(line['amount_rub'])), 'shared-expense-not-in-canonical-cost')
    return json.loads(json.dumps({key:{field:value for field,value in row.items() if field in {'invoice_no','capital_rub','quantity','blockers','component_controls','financial_readiness','controls'}} for key,row in proof.items()},default=str))


def ownership(before, parsed_dt):
    annex = parsed_dt['normalized_parse']['annex_items']; by_barcode = {}
    for row in annex:
        match = re.search(r'ШТРИХКОД\s*[-:]?\s*(\d+)', row['source_name'])
        require(match is not None and match[1] not in by_barcode, 'shared-annex-barcode-ambiguous')
        by_barcode[match[1]] = row
    owners = {}
    for name, tables in before.items():
        header = tables['supplier_shipments'][0]
        product = [r for r in tables['supplier_shipment_lines'] if r['line_type'] == 'product']
        quantities = {}; value = Decimal(0); quantity = Decimal(0)
        for line in product:
            row = by_barcode.get(line['barcode'])
            require(row is not None, 'shared-order-barcode-missing-from-DT')
            nm = re.search(r'NMID\s*[-:]?\s*(\d+)', row['source_name'])
            require(nm is not None and int(nm[1]) == int(line['internal_nm_id']), 'shared-order-nm-mismatch')
            number = str(row['annex_row_number'])
            quantities[number] = quantities.get(number, 0) + line['qty']
            value += Decimal(str(line['amount'])); quantity += Decimal(str(line['qty']))
        require(value == Decimal(str(header['product_amount_total'])) and quantity == Decimal(str(header['product_qty_total'])), 'shared-owner-line-total-mismatch')
        owners[name] = {'quantity':str(quantity), 'invoice_value_cny':str(value), 'invoice_total_cny':str(header['invoice_amount_total']), 'annex_quantities':quantities}
    return owners


def candidate(before, sources, timestamp):
    from packages.application.supplier_financial_documents import _expense_line_for_storage
    owners = ownership(before, sources['dt']['parsed']); result=[]
    for key in ('dt','211','212','226'):
        source = sources[key]
        shares = allocate_shared_document(source['parsed'], source_sha256=source['sha256'], source_filename=source['filename'], owners=owners)
        for name, parsed in shares.items():
            identity = IDS[name]; meta=parsed['normalized_parse']
            doc_id='fdoc_shared_724725_'+key+'_'+name[-3:]
            path=str(Path('supplier_financial_documents/files')/identity/doc_id/source['filename'])
            document={k:meta.get(k) for k in ('document_type','document_number','document_date','vendor','currency','total_amount','total_amount_rub','vat_rate','vat_amount_rub','due_date','route','contract_ref')}
            document.update(document_id=doc_id,supplier_order_id=identity,original_filename=source['filename'],stored_file_path=path,file_content_type='application/pdf',file_sha256=source['sha256'],uploaded_at=timestamp,updated_at=timestamp,parse_status='confirmed',raw_parse=parsed['raw_parse'],normalized_parse=meta,parser_version=parsed['parser_version'],errors=[],warnings=[*parsed.get('warnings',[]), 'Общий документ 26GN724/26GN725: здесь учтена только доля '+name+'. Исходный PDF содержит общую сумму. Доли сверены в совокупности; повторно полную сумму не вводить.', 'Номера CMR в исходных документах различаются; принадлежность груза подтверждена перепиской с логистом.'])
            lines=[]
            for index,line in enumerate(parsed['expense_lines'],1):
                item=_expense_line_for_storage({**line,'line_id':doc_id+'_line_'+str(index),'status':'confirmed'},supplier_order_id=identity,document_id=doc_id,sort_order=index)
                lines.append(item)
            result.append({'document':document,'expense_lines':lines,'source_key':key})
    require(sum((Decimal(str(l['amount_rub'])) for item in result for l in item['expense_lines']),Decimal(0)) == Decimal('2655229.97'), 'shared-total-not-conserved')
    return result


def durable(path, value):
    with path.open('xb') as stream:
        stream.write(encoded(value)); stream.flush(); os.fsync(stream.fileno())


class SupplierSharedCustomsAllocationAdapter:
    def context(self, request):
        require(socket.gethostname() == request['hostname'], 'shared-target-host-mismatch')
        root=Path(request['runtime_dir']).resolve()
        require(Path(request['runtime_sha_file']).read_text().strip() == request['runtime_sha'], 'shared-runtime-sha-mismatch')
        return root, StoreRegistry(root).resolve('operational')

    def sources(self, request, root):
        from packages.application.supplier_financial_documents import parse_financial_document_pdf
        require(set(request['sources']) == {'dt','211','212','226'}, 'shared-source-scope-invalid')
        result={}
        for key,item in request['sources'].items():
            path=Path(item['path']).resolve()
            require(path.is_relative_to(root) and path.suffix.lower()=='.pdf', 'shared-source-path-invalid')
            data=path.read_bytes(); sha=hashlib.sha256(data).hexdigest()
            require(sha == item['sha256'] and len(data)<20*1024*1024, 'shared-source-hash-invalid')
            parsed=parse_financial_document_pdf(data,filename=path.name)
            require(not parsed.get('errors'), 'shared-source-parser-errors')
            result[key]={'filename':path.name,'sha256':sha,'parsed':parsed}
        return result

    def plan(self, request):
        root,db=self.context(request); sources=self.sources(request,root)
        with closing(connect(db)) as conn:
            conn.execute('BEGIN'); before=capture(conn)
            all_docs=rows(conn,PREFIX+'supplier_financial_documents','1=1',())
        after=candidate(before,sources,request['timestamp'])
        hashes={s['sha256'] for s in sources.values()}
        require(not any(d['file_sha256'] in hashes or (d['document_type']=='customs_declaration' and d['document_number']=='10323020/071026/5052442') or (d['document_type']=='logistics_invoice' and d['document_number'] in {'211','212','226'} and d['document_date'] in {'2026-10-02','2026-10-20'}) for d in all_docs), 'shared-source-already-imported')
        require(not any(d['document_id'] in {a['document']['document_id'] for a in after} for d in all_docs), 'shared-document-id-conflict')
        require(all(not tables['supplier_shipments'][0]['expenses_complete'] for tables in before.values()), 'shared-certified-order-requires-separate-correction')
        cost_proof(before,after)
        return root,db,before,after

    def preview(self, request, operation_id):
        root,db,before,after=self.plan(request)
        return {'operation_id':operation_id,'target':request['hostname']+':'+str(db),'scope':{'orders':IDS,'documents':8,'expense_rows':12,'total_rub':'2655229.97'},'prestate_sha256':digest(before),'candidate_sha256':digest(after),'recovery':{'before_image':str(root/'shared_customs_allocations'/operation_id/'before.json'),'candidate':str(root/'shared_customs_allocations'/operation_id/'candidate.json'),'method':'SupplierSharedCustomsAllocationAdapter.recover: remove-only-exact-new-documents-and-owned-files; invalidate-and-resume-preparation; verify-original-business-sources'}}

    def apply(self, request, operation_id, preview):
        from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
        from packages.application.warehouse_functional_lock import warehouse_functional_write_lock
        root,_=self.context(request)
        with warehouse_functional_write_lock(root,timeout_seconds=45):
            root,db,before,after=self.plan(request)
            require(digest(before)==preview['prestate_sha256'] and digest(after)==preview['candidate_sha256'], 'shared-prestate-or-candidate-drift')
            evidence=root/'shared_customs_allocations'/operation_id
            evidence.mkdir(parents=True,exist_ok=False)
            durable(evidence/'before.json',before); durable(evidence/'candidate.json',after)
            durable(evidence/'cost-proof.json',cost_proof(before,after))
            runtime=RegistryUploadDbBackedRuntime(runtime_dir=root)
            try:
                for item in after:
                    doc=item['document']; path=root/doc['stored_file_path']
                    path.parent.mkdir(parents=True,exist_ok=True)
                    data=Path(request['sources'][item['source_key']]['path']).read_bytes()
                    require(hashlib.sha256(data).hexdigest()==doc['file_sha256'],'shared-source-changed-during-submit')
                    with path.open('xb') as stream:
                        stream.write(data);stream.flush();os.fsync(stream.fileno())
                    runtime.save_supplier_financial_document(document=doc,expense_lines=item['expense_lines'])
                durable(evidence/'submitted.json',{'operation_id':operation_id,'candidate_sha256':digest(after)})
            except Exception as exc:
                raise AmbiguousSubmit('shared-import-partial-readback-required') from exc
        return {'operation_id':operation_id,'disposition':'submitted'}

    def readback(self, request, operation_id):
        from packages.application.registry_upload_db_backed_runtime import _supplier_financial_document_to_dict, _supplier_financial_expense_line_to_dict
        root,db=self.context(request); evidence=root/'shared_customs_allocations'/operation_id
        if not evidence.exists(): return {'operation_id':operation_id,'state':'not_submitted'}
        if not (evidence/'candidate.json').is_file():return {'operation_id':operation_id,'state':'ambiguous'}
        after=json.loads((evidence/'candidate.json').read_text()); mismatches=[]
        with closing(connect(db)) as conn:
            conn.execute('BEGIN')
            for item in after:
                doc=item['document']; record=conn.execute('SELECT * FROM '+PREFIX+'supplier_financial_documents WHERE document_id=?',(doc['document_id'],)).fetchone()
                if record is None: mismatches.append(doc['document_id']);continue
                saved=_supplier_financial_document_to_dict(record)
                if any(saved.get(k)!=('' if v is None and k in {'vendor','due_date','route','contract_ref'} else v) for k,v in doc.items()): mismatches.append(doc['document_id']+':document')
                actual=[_supplier_financial_expense_line_to_dict(r) for r in conn.execute('SELECT * FROM '+PREFIX+'supplier_financial_expense_lines WHERE financial_document_id=? ORDER BY sort_order',(doc['document_id'],))]
                if len(actual)!=len(item['expense_lines']) or any(any(a.get(k)!=v for k,v in b.items()) for a,b in zip(actual,item['expense_lines'])):mismatches.append(doc['document_id']+':expenses')
                path=root/doc['stored_file_path']
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=doc['file_sha256']:mismatches.append(doc['document_id']+':attachment')
            current=capture(conn)
            before=json.loads((evidence/'before.json').read_text())
            ids={item['document']['document_id'] for item in after}
            for name,tables in current.items():
                for table,records in tables.items():
                    if table=='supplier_preparation_intents':continue
                    filtered=[r for r in records if r.get('document_id',r.get('financial_document_id')) not in ids]
                    if filtered != before[name][table]:mismatches.append(name+':protected:'+table)
        return {'operation_id':operation_id,'state':'ambiguous' if mismatches else 'applied','mismatches':mismatches,'document_count':len(after),'total_rub':'2655229.97'}

    def recover(self, request, operation_id):
        """Exact candidate cleanup followed by domain re-preparation, never resubmit."""
        from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
        from packages.application.supplier_preparation_intents import resume_supplier_preparation
        from packages.application.warehouse_functional_lock import warehouse_functional_write_lock
        root,db=self.context(request); evidence=root/'shared_customs_allocations'/operation_id
        after=json.loads((evidence/'candidate.json').read_text())
        candidate_ids={item['document']['document_id'] for item in after}
        with warehouse_functional_write_lock(root,timeout_seconds=45):
            check=self.readback(request,operation_id)
            require(all(m in candidate_ids for m in check.get('mismatches',[])), 'shared-recovery-source-drift')
            for item in after:
                path=root/item['document']['stored_file_path']
                if path.exists():
                    require(hashlib.sha256(path.read_bytes()).hexdigest()==item['document']['file_sha256'],'shared-recovery-file-drift')
            durable(evidence/'recovery-started.json',check)
            runtime=RegistryUploadDbBackedRuntime(runtime_dir=root)
            for item in reversed(after):
                doc=item['document']
                runtime.delete_supplier_financial_document(supplier_order_id=doc['supplier_order_id'],document_id=doc['document_id'])
                path=root/doc['stored_file_path']
                if path.exists():
                    require(hashlib.sha256(path.read_bytes()).hexdigest()==doc['file_sha256'],'shared-recovery-file-drift')
                    path.unlink()
            with closing(connect(db)) as conn:
                current=capture(conn)
            before=json.loads((evidence/'before.json').read_text())
            require(all(rows==before[name][table] for name,tables in current.items() for table,rows in tables.items() if table!='supplier_preparation_intents'), 'shared-recovery-business-source-drift')
        results={name:resume_supplier_preparation(runtime,identity) for name,identity in IDS.items()}
        durable(evidence/'recovered.json',{'operation_id':operation_id,'business_sources_restored':True,'preparation':results})
        return {'operation_id':operation_id,'business_sources_restored':True,'preparation':results}
