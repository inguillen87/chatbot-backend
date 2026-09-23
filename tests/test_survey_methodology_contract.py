from copy import deepcopy
import unittest
from services.survey_methodology_contract import (
    WRITE_CONTRACT, blank_fields, validate_fields, validate_write, coverage, canonical_digest, MethodologyInputError,
)


def request(fields=None,expected=0,instrument=1):
    return {'contract_version':WRITE_CONTRACT,'expected_revision':expected,'expected_instrument_revision':instrument,
            'fields':fields or blank_fields(),'change_reason':'Registro inicial de las fuentes.'}


class MethodologyContractTests(unittest.TestCase):
    def test_missing_information_remains_missing(self):
        fields=blank_fields(); result=coverage(fields)
        self.assertEqual(result['documented'],0);self.assertEqual(result['total'],16)
        self.assertFalse(result['inference_authorized']);self.assertEqual(fields['design'],'unknown')

    def test_all_fields_documented_do_not_certify_inference(self):
        fields={k:'Declarado' for k in blank_fields()};fields.update(design='probability',fieldwork_start='2026-09-01',fieldwork_end='2026-09-22')
        result=coverage(validate_fields(fields));self.assertEqual(result['documented'],16)
        self.assertEqual(result['assessment'],'self_declared');self.assertFalse(result['inference_authorized'])

    def test_valid_partial_document_and_reason(self):
        fields=blank_fields();fields['purpose']='Medir necesidades de servicios'
        self.assertEqual(validate_write(request(fields))['fields']['purpose'],fields['purpose'])

    def test_partial_dates_are_allowed_without_inventing_the_other_date(self):
        fields=blank_fields();fields['fieldwork_start']='2026-09-22'
        self.assertEqual(validate_fields(fields)['fieldwork_end'],'')

    def test_invalid_dates_and_reversed_intervals(self):
        for value in ['2026-02-29','2026-13-01','22/09/2026','2026-9-1','0000-01-01']:
            fields=blank_fields();fields['fieldwork_start']=value
            with self.subTest(value=value),self.assertRaises(MethodologyInputError):validate_fields(fields)
        fields=blank_fields();fields.update(fieldwork_start='2026-09-22',fieldwork_end='2026-09-01')
        with self.assertRaises(MethodologyInputError):validate_fields(fields)

    def test_unknown_or_missing_fields_are_not_silently_dropped(self):
        for fields in [{}, {**blank_fields(),'tenant_id':'1'}, {k:v for k,v in blank_fields().items() if k!='purpose'}]:
            with self.subTest(fields=fields),self.assertRaises(MethodologyInputError):validate_fields(fields)

    def test_numbers_objects_null_and_booleans_are_not_text(self):
        for value in [False,12,None,{},[]]:
            fields=blank_fields();fields['purpose']=value
            with self.subTest(value=value),self.assertRaises(MethodologyInputError):validate_fields(fields)

    def test_strings_are_bounded_and_control_characters_rejected(self):
        for value in ['x'*1001,'hidden\x00text','reverse\u202etext']:
            fields=blank_fields();fields['purpose']=value
            with self.subTest(value=value),self.assertRaises(MethodologyInputError):validate_fields(fields)

    def test_text_is_normalized_without_interpreting_markup(self):
        fields=blank_fields();fields['purpose']='  Cafe\u0301\r\n<img src=x>  '
        self.assertEqual(validate_fields(fields)['purpose'],'Caf\u00e9\n<img src=x>')

    def test_unknown_design_is_never_probability_by_default(self):
        fields=blank_fields();fields['design']='certified'
        with self.assertRaises(MethodologyInputError):validate_fields(fields)

    def test_expected_revisions_are_strict(self):
        for key,values in [('expected_revision',[-1,True,'1',1.5,2**31]),('expected_instrument_revision',[0,False,'1',None])]:
            for value in values:
                body=request();body[key]=value
                with self.subTest(key=key,value=value),self.assertRaises(MethodologyInputError):validate_write(body)

    def test_unknown_write_keys_and_contracts_rejected(self):
        for body in [{**request(),'approved':True},{**request(),'contract_version':'unknown'},None,[]]:
            with self.subTest(body=body),self.assertRaises(MethodologyInputError):validate_write(body)

    def test_reason_required_even_for_partially_filled_profile(self):
        for reason in ['', 'short', 'x'*501, True]:
            body=request();body['change_reason']=reason
            with self.subTest(reason=reason),self.assertRaises(MethodologyInputError):validate_write(body)

    def test_normalization_does_not_mutate_request(self):
        body=request();copy=deepcopy(body);validate_write(body);self.assertEqual(body,copy)

    def test_digest_is_canonical_and_changes_with_methodology(self):
        one=request();two=dict(reversed(list(one.items())))
        self.assertEqual(canonical_digest(one),canonical_digest(two))
        two['fields']={**two['fields'],'purpose':'Changed purpose'}
        self.assertNotEqual(canonical_digest(one),canonical_digest(two))


if __name__=='__main__':unittest.main(verbosity=2)
