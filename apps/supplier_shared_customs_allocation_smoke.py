from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
import unittest
from copy import deepcopy
from decimal import Decimal

from packages.application.supplier_shared_document_allocation import allocate_shared_document, CUSTOMS, is_allocated_document, OWNER_IDS


class SharedDocumentAllocationTest(unittest.TestCase):
    def setUp(self):
        self.owners = {'26GN724': {'quantity': 34000, 'invoice_value_cny': 211625, 'invoice_total_cny': 223525, 'annex_quantities': {'1': 34000}}, '26GN725': {'quantity': 24750, 'invoice_value_cny': 160500, 'invoice_total_cny': 169162.5, 'annex_quantities': {'1': 24750}}}
        total = float(sum((v[1] for v in CUSTOMS.values()), Decimal(0)))
        self.parsed = {'parser_version': 'original', 'raw_parse': {'original_text': 'untouched'}, 'normalized_parse': {'document_type': 'customs_declaration', 'declaration_number': '10323020/071026/5052442', 'invoice_amount_cny': 392687.5, 'total_amount': total, 'total_amount_rub': total, 'total_customs_value_rub': 1000000, 'customs_gross_weight_kg': 1200, 'goods_items': [{'position_number': '1', 'quantity': 4374, 'unit': 'КГ'}], 'annex_items': [{'annex_row_number': '1', 'parent_position_number': '1', 'quantity': 58750, 'barcode': 'shared-sku'}]}, 'expense_lines': []}
        for category, (key, amount, _) in CUSTOMS.items():
            self.parsed['normalized_parse'][key] = float(amount)
            self.parsed['expense_lines'].append({'category': category, 'amount': float(amount), 'amount_rub': float(amount), 'currency': 'RUB', 'status': 'parsed', 'vat_amount_rub': float(amount) if category == 'import_vat_5010' else None})

    def allocate(self, parsed=None, owners=None):
        return allocate_shared_document(parsed or self.parsed, source_sha256='a'*64, source_filename='original.pdf', owners=owners or self.owners)

    def test_shared_sku_conservation_provenance_and_no_input_mutation(self):
        before = deepcopy(self.parsed)
        result = self.allocate()
        self.assertEqual(self.parsed, before)
        self.assertEqual([line['amount_rub'] for line in result['26GN724']['expense_lines']], [12352.27, 292553.80, 707980.18])
        for index, original in enumerate(self.parsed['expense_lines']):
            self.assertEqual(sum((Decimal(str(result[o]['expense_lines'][index]['amount_rub'])) for o in self.owners), Decimal(0)), Decimal(str(original['amount_rub'])))
        for owner, quantity in [('26GN724',34000), ('26GN725',24750)]:
            meta = result[owner]['normalized_parse']
            self.assertEqual(meta['invoice_amount_cny'], self.owners[owner]['invoice_total_cny'])
            self.assertEqual(meta['annex_items'][0]['quantity'], quantity)
            self.assertEqual(meta['goods_items'][0]['quantity'], quantity)
            self.assertEqual(meta['goods_items'][0]['unit'], 'ШТ')
            self.assertEqual(meta['goods_items'][0]['source_declared_unit'], 'КГ')
            self.assertEqual(meta['goods_items'][0]['source_declared_quantity'], 4374)
            self.assertEqual(meta['annex_items'][0]['source_declared_quantity'],58750)
            self.assertEqual(meta['shared_document_allocation']['source_normalized_parse'], before['normalized_parse'])
            self.assertEqual(result[owner]['raw_parse'], before['raw_parse'])

    def test_reject_bad_annex_and_double_allocation(self):
        bad = deepcopy(self.owners)
        bad['26GN725']['annex_quantities']['1'] -= 1
        with self.assertRaises(ValueError): self.allocate(owners=bad)
        with self.assertRaises(ValueError): self.allocate(parsed=self.allocate()['26GN724'])

    def test_reject_changed_totals_or_basis(self):
        bad = deepcopy(self.parsed)
        bad['expense_lines'][0]['amount_rub'] += .01
        with self.assertRaises(ValueError): self.allocate(parsed=bad)
        bad_owners = deepcopy(self.owners)
        bad_owners['26GN724']['invoice_value_cny'] += 1
        with self.assertRaises(ValueError): self.allocate(owners=bad_owners)

    def test_reject_full_invoice_total_used_as_product_basis(self):
        owners = deepcopy(self.owners)
        owners['26GN724']['invoice_value_cny'] = owners['26GN724']['invoice_total_cny']
        with self.assertRaises(ValueError): self.allocate(owners=owners)
        owners = deepcopy(self.owners)
        owners['26GN725']['invoice_total_cny'] = owners['26GN725']['invoice_value_cny']
        with self.assertRaises(ValueError): self.allocate(owners=owners)

    def test_logistics_exact_residual(self):
        for number, amount, first in [('211',113544,65710.57),('212',5000,2893.62),('226',755986,437506.79)]:
            parsed = {'raw_parse': {'original': True}, 'normalized_parse': {'document_type':'logistics_invoice','document_number':number,'total_amount':amount,'total_amount_rub':amount}, 'expense_lines':[{'category':'logistics','currency':'RUB','amount':amount,'amount_rub':amount}]}
            result = self.allocate(parsed=parsed)
            self.assertEqual(result['26GN724']['expense_lines'][0]['amount_rub'],first)
            self.assertEqual(sum((Decimal(str(result[o]['expense_lines'][0]['amount_rub'])) for o in self.owners), Decimal(0)),Decimal(amount))

    def test_refresh_marker_checks_owner_and_source(self):
        parsed = self.allocate()['26GN724']
        doc = {'normalized_parse': parsed['normalized_parse'], 'supplier_order_id': OWNER_IDS['26GN724'], 'file_sha256': 'a'*64}
        self.assertTrue(is_allocated_document(doc))
        from packages.application.supplier_financial_documents import SupplierFinancialDocumentsBlock
        block=object.__new__(SupplierFinancialDocumentsBlock)
        doc['parser_version']='old-version'
        doc['stored_file_path']='original.pdf'
        self.assertFalse(block._saved_document_needs_parse_refresh(doc))
        self.assertFalse(is_allocated_document({}))
        doc['supplier_order_id'] = OWNER_IDS['26GN725']
        with self.assertRaises(ValueError): is_allocated_document(doc)
        doc['supplier_order_id'] = OWNER_IDS['26GN724']
        doc['file_sha256'] = 'b'*64
        with self.assertRaises(ValueError): is_allocated_document(doc)

    def test_reject_nonfinite_amount(self):
        parsed = deepcopy(self.parsed)
        parsed['expense_lines'][0]['amount_rub'] = float('nan')
        with self.assertRaises(ValueError): self.allocate(parsed=parsed)

if __name__ == '__main__': unittest.main()
