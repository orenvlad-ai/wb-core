"""Anonymized structure fixtures; no private original bank document in Git."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from packages.application.cny_ledger import parse_cny_document_text, parse_cny_document_pdf
from packages.application.supplier_financial_documents import parse_financial_document_text
from apps.cny_ledger_smoke import PAYMENT_TEXT

FIXTURE = '''0401060
ПЛАТЕЖНОЕ ПОРУЧЕНИЕ №
Дата Вид платежа
28.10.202624
Сумма
прописью Четыреста сорок семь тысяч семьсот шестнадцать китайских юаней 25 цзяо
Сумма 447 716.25
Сч. № 40802156000000000001
БИК VTBRRUMMXXX
Банк плательщика
БИК VTBRCNSHXXX
Банк получателя
Сч. № 40807156000000000002
CONTRACT FR-001/26 DD 08.06.2026 /PYTR/GOD/
Поступ. в банк плат.
28.10.2026
Списано со сч. плат.
28.10.2026
IE TEST CUSTOMER TEST ADDRESS
VTB BANK (PJSC) MOSCOW RU
VTB BANK (PJSC) SHANGHAI BRANCH
GUANGZHOU TEST TECHNOLOGY CO., LTD
TEST ADDRESS GUANGZHOU CN
28 октября 2026
ИСПОЛНЕНО
'''

class OriginalPaymentTest(unittest.TestCase):
    def test_original_structure_flat_and_layout(self):
        for text in [FIXTURE, FIXTURE.replace('Дата Вид платежа\n28.10.202624','24  28.10.2026\nДата Вид платежа')]:
            parsed=parse_cny_document_text(text)
            self.assertEqual(parsed['errors'],[])
            n=parsed['normalized_parse']
            self.assertEqual((n['document_type'],n['document_number'],n['operation_date'],n['cny_amount']),('supplier_cny_payment','24','2026-10-28',447716.25))
            self.assertEqual((n['contract_number'],n['contract_date']),('FR-001/26','2026-06-08'))
            self.assertEqual(n['beneficiary_customer'],'GUANGZHOU TEST TECHNOLOGY CO., LTD')
            self.assertEqual(n['operation_datetime'],'')
            self.assertEqual(n['execution_datetime_precision'],'date_only')
            financial=parse_financial_document_text(text)
            self.assertEqual(financial['normalized_parse']['document_type'],'bank_transfer_application')
            self.assertEqual(financial['expense_lines'],[])

    def test_reject_unexecuted_rub_missing_and_ambiguous(self):
        bad=[FIXTURE+'Валюта RUB\n',FIXTURE.replace('ИСПОЛНЕНО','НЕ ИСПОЛНЕНО'),FIXTURE.replace('ИСПОЛНЕНО','ПРИНЯТО'),FIXTURE.replace('китайских юаней','российских рублей'),FIXTURE.replace('40802156','40802810'),FIXTURE.replace('CONTRACT FR-001/26 DD 08.06.2026 /PYTR/GOD/',''),FIXTURE+'Сумма 447 716.25\n',FIXTURE+FIXTURE,FIXTURE.replace('28 октября 2026','29 октября 2026'),FIXTURE.replace('GUANGZHOU TEST TECHNOLOGY CO., LTD',''),FIXTURE.replace('447 716.25','0.00')]
        for text in bad:
            parsed=parse_cny_document_text(text)
            self.assertTrue(parsed['errors'],text)
            self.assertEqual(parsed['normalized_parse'],{})

    def test_generic_company_name_and_ambiguous_beneficiary(self):
        parsed=parse_cny_document_text(FIXTURE.replace('GUANGZHOU TEST TECHNOLOGY CO., LTD','SHENZHEN OTHER TECHNOLOGY CO., LTD'))
        self.assertEqual(parsed['errors'],[])
        self.assertEqual(parsed['normalized_parse']['beneficiary_customer'],'SHENZHEN OTHER TECHNOLOGY CO., LTD')
        parsed=parse_cny_document_text(FIXTURE+'SHENZHEN OTHER TECHNOLOGY CO., LTD\n')
        self.assertTrue(parsed['errors'])
        self.assertEqual(parsed['normalized_parse'],{})

    def test_pdf_entrypoint_uses_original_text_without_synthetic_mt103(self):
        original=b'%PDF original content retained by upload layer'
        seen=[]
        def extractor(data, filename):
            seen.append(data)
            return FIXTURE,{'method':'fixture'},[]
        parsed=parse_cny_document_pdf(original,filename='original.pdf',text_extractor=extractor)
        self.assertEqual(seen,[original])
        self.assertEqual(parsed['normalized_parse']['cny_amount'],447716.25)
        self.assertEqual(parsed['raw_parse']['filename'],'original.pdf')

    def test_existing_mt103_regression(self):
        parsed=parse_cny_document_text(PAYMENT_TEXT)
        self.assertEqual(parsed['errors'],[])
        n=parsed['normalized_parse']
        self.assertEqual((n['document_type'],n['cny_amount'],n['operation_date']),('supplier_cny_payment',785087.5,'2026-05-13'))
        self.assertEqual(n['transfer_application_number'],'1')
        self.assertNotIn('source_document_format',n)

if __name__ == '__main__': unittest.main()
