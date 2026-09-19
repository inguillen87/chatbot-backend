import unittest
from xml.etree.ElementTree import fromstring
from services.accessible_support_guide import load_guide, menu_response, whatsapp_text, whatsapp_twiml

class AccessibleSupportGuideTests(unittest.TestCase):
    def test_all_nodes_are_reachable_from_the_start(self):
        guide=load_guide(); seen=set(); pending=[guide['start']]
        while pending:
            node=pending.pop()
            if node in seen: continue
            seen.add(node); pending.extend(a['target'] for a in guide['nodes'][node]['actions'])
        self.assertEqual(seen,set(guide['nodes']))
    def test_five_thematic_areas_and_human_help(self):
        self.assertEqual([a['code'] for a in menu_response('main')['actions']], ['1','2','3','4','5','0'])
    def test_no_personal_data_or_operations_enabled(self):
        guide=load_guide()
        self.assertFalse(any(guide['policy'].values()))
        self.assertEqual(guide['source']['page_count'],14)
    def test_option_navigation(self):
        self.assertEqual(menu_response('main','1')['id'],'documentation')
        self.assertEqual(menu_response('documentation','1')['id'],'cud-checklist')
    def test_home_navigation(self):
        for code in ('hola','inicio'):
            self.assertEqual(menu_response('health',code)['id'],'start')
        self.assertEqual(menu_response('health','menu')['id'],'main')
    def test_feedback_questions_stay_separate(self):
        self.assertEqual(menu_response('feedback-resolution','2')['id'],'feedback-ease')
        self.assertEqual(menu_response('feedback-ease','5')['id'],'thanks')
    def test_human_handoff_is_explicitly_not_sent(self):
        self.assertIn('no enviado',menu_response('handoff-example')['title'].lower())
        self.assertIn('sin ticket creado',menu_response('handoff-example')['text'])
    def test_arbitrary_text_is_not_processed_as_personal_information(self):
        for text in ('12345678','my diagnosis','../../secret'):
            with self.assertRaises(ValueError): menu_response('main',text)
    def test_unknown_node_fails(self):
        with self.assertRaises(ValueError): menu_response('__proto__')
    def test_twiml_round_trip_contains_the_same_response(self):
        for key in load_guide()['nodes']:
            text=whatsapp_text(key)
            self.assertLess(len(text),1600)
            self.assertEqual(fromstring(whatsapp_twiml(key)).findtext('Message'),text)
    def test_responses_are_independent_copies(self):
        item=menu_response('main'); item['actions'].clear()
        self.assertEqual(len(menu_response('main')['actions']),6)
    def test_source_has_traceable_hash(self):
        self.assertEqual(load_guide()['source']['sha256'],'1f6de63d4f70ede4e967077cc9eae768c64574b022d66c5ebb8868aac3d4a6ee')

if __name__=='__main__': unittest.main()
