"""Authenticated source attachments for already accepted FF service uploads.

Invoice originals belong to the immutable historical effect recipe. They are
not another expense or a replacement for the service XLSX/payment validation.
All functions read existing native rows/files only.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sqlite3
from urllib.parse import quote

from packages.application import operator_fulfillment_services as receipts
from packages.application import fbs_historical_fbo_publication as typed
from packages.application.fbs_historical_fbo_action import _directory
from packages.application.ff_pool_documents import REQUESTS_TABLE

MAX_FILE_BYTES=16*1024**2
MAX_RECIPE_BYTES=160*1024**2
BASE='/v1/sheet-vitrina-v1/supply/fulfillment-services/uploads/'


def require(value):
    if not value:raise ValueError('fulfillment_original_invoice_unavailable')


def _file(path, digest, *, folder):
    path=Path(path)
    # Only hash-named artifacts inside this exact operation's retained bundle.
    expected=folder/(digest+'.source')
    require(path.resolve()==expected.resolve() and not folder.is_symlink() and not path.is_symlink())
    require(expected.parent==folder and expected.name==digest+'.source')
    fd=os.open(expected,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and not info.st_mode&0o077 and 0<info.st_size<=MAX_FILE_BYTES)
        with os.fdopen(fd,'rb',closefd=False) as handle:data=handle.read(MAX_FILE_BYTES+1)
    finally:os.close(fd)
    require(len(data)==info.st_size and hashlib.sha256(data).hexdigest()==digest and data.startswith(b'%PDF-'))
    return data


def original_invoice(conn, *, runtime_dir, upload_id):
    """Return (public metadata, verified bytes), or None for ordinary uploads."""
    require(re.fullmatch(r'[A-Za-z0-9_-]{1,120}',str(upload_id)) is not None)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_ready_publications'").fetchone():return None
    if not receipts._exists(conn,receipts.REQUESTS):return None
    upload=conn.execute('SELECT * FROM sheet_vitrina_v1_fulfillment_service_uploads WHERE upload_id=?',(upload_id,)).fetchone()
    require(upload and upload['validation_status']=='ok' and not upload['deleted_at'])
    bound=conn.execute(f'SELECT * FROM {receipts.REQUESTS} WHERE upload_id=? AND action=\'upload\'',(upload_id,)).fetchall()
    require(len(bound)<=8)
    matches=[]
    for request in bound:
        headers=conn.execute("SELECT operation_id,attempt_id,length(CAST(inputs_json AS BLOB)) bytes FROM sheet_vitrina_v1_ready_publications "
            "WHERE kind=? AND EXISTS(SELECT 1 FROM json_each(inputs_json,'$.original_documents') doc "
            "WHERE json_extract(doc.value,'$.xlsx_request_scope')=? AND json_extract(doc.value,'$.xlsx_request_id')=?) LIMIT 2",
            (typed.EFFECT_CONTRACT,request['request_scope'],request['request_id'])).fetchall()
        require(len(headers)<=1)
        for header in headers:
            require(header['bytes']<=MAX_RECIPE_BYTES)
            raw=conn.execute('SELECT inputs_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                (header['operation_id'],header['attempt_id'])).fetchone()
            stored=json.loads(raw[0])
            binding=dict(operation_id=header['operation_id'],attempt_id=header['attempt_id'],recipe_digest=stored['recipe_digest'])
            recipe=typed.read_effect_intent(conn,binding=binding)
            docs=[d for d in recipe.get('original_documents',[]) if d.get('xlsx_request_scope')==request['request_scope']
                and d.get('xlsx_request_id')==request['request_id']]
            require(len(docs)==1)
            doc=docs[0]
            fees=[f for f in recipe['fee_requests'] if f['request_scope']==request['request_scope'] and f['request_id']==request['request_id']]
            require(len(fees)==1 and doc['xlsx_sha256']==fees[0]['file_sha256']==upload['file_sha256']==request['payload_digest']
                and doc['wb_supply_id']==fees[0]['wb_supply_id'])
            lines=conn.execute('SELECT matched_wb_supply_id,matched_wb_cache_key,match_status FROM sheet_vitrina_v1_fulfillment_service_lines WHERE upload_id=?',(upload_id,)).fetchall()
            require(lines and all(r['match_status']=='ok' and str(r['matched_wb_supply_id'])==doc['wb_supply_id']
                and r['matched_wb_cache_key']=='supply:'+doc['wb_supply_id'] for r in lines))
            receipt=conn.execute(f'SELECT * FROM {receipts.TABLE} WHERE operation_id=?',(request['operation_id'],)).fetchone()
            source={**receipts._source(conn,upload_id),'action':'upload','source_revision':upload['file_sha256']}
            require(receipt and receipt['upload_id']==upload_id and receipt['action']=='upload'
                and receipt['source_revision']==upload['file_sha256'] and receipt['source_digest']==receipts.digest(source)
                and json.loads(receipt['source_json'])==source)
            payment=conn.execute(f'SELECT state,error_code,posted_document_id,source_sha256 FROM {REQUESTS_TABLE} WHERE request_id=?',
                (doc['payment_request_id'],)).fetchone()
            require(payment and payment['state']=='blocked' and payment['error_code']=='overhead_confirmation_cancelled'
                and not payment['posted_document_id'] and payment['source_sha256']==doc['payment_source_sha256'])
            digest=doc['invoice_sha256'];require(re.fullmatch('[0-9a-f]{64}',digest) is not None)
            folder=_directory(Path(runtime_dir),recipe['operation_id'])/'reviewed-sources'
            data=_file(doc['invoice_artifact_path'],digest,folder=folder)
            matches.append((dict(invoice_no=str(doc['invoice_no']),sha256=digest,wb_supply_id=doc['wb_supply_id'],
                path=BASE+quote(upload_id,safe='')+'/original-invoice.pdf',
                payment_request_id=doc['payment_request_id'],
                payment_detail_path='/v1/sheet-vitrina-v1/warehouses/ff/facility-pools/requests/'+quote(doc['payment_request_id'],safe='')),data))
    require(len(matches)<=1)
    return matches[0] if matches else None


def public_invoice(conn, *, runtime_dir, upload_id):
    try:
        result=original_invoice(conn,runtime_dir=runtime_dir,upload_id=upload_id)
        return dict(original_invoice=result[0] if result else None,original_invoice_status='available' if result else 'not_attached')
    except (ValueError,OSError,KeyError,TypeError,sqlite3.Error):
        # A corrupt optional attachment must not hide the accepted expense.
        return dict(original_invoice=None,original_invoice_status='unavailable')
