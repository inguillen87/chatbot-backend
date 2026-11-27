"""Reserve selection: published nodes by word match when the interpreter is down."""
import unittest

from services.institutional_assistant_content import ContentError, reserve_selection, select_nodes


def _node(key, title, text, actions=()):
    return {'id': key, 'title': title, 'text': text,
            'actions': [{'code': str(i + 1), 'label': label, 'target': target}
                        for i, (label, target) in enumerate(actions)]}


BUNDLE = {'start': 'start', 'nodes': {
    'start': _node('start', '¿Qué necesitás?', 'Elegí un tema.',
                   [('Certificado CUD', 'cud'), ('Trabajo y cursos', 'work'), ('Contactos', 'contacts')]),
    'cud': _node('cud', 'Documentación del CUD', 'Requisitos para iniciar el certificado.',
                 [('Vigencia', 'cud-validity'), ('Volver', 'start')]),
    'cud-validity': _node('cud-validity', 'Vigencia y avisos', 'El CUD tiene fecha de vencimiento.'),
    'work': _node('work', 'Trabajo y cursos', 'Registro de inclusión laboral y ayuda con el CV.'),
    'contacts': _node('contacts', 'Oficinas en Río Grande', 'Teléfono 2964-000000. Atención presencial.'),
}}


def _down(*_args):
    raise RuntimeError('provider down')


class ReserveSelectionTest(unittest.TestCase):
    def test_words_of_the_question_land_on_a_published_node(self):
        self.assertEqual(reserve_selection(BUNDLE, '¿Cuáles son los requisitos del CUD?', 'start')[0], 'cud')
        self.assertEqual(reserve_selection(BUNDLE, 'necesito ayuda con mi curriculum para trabajar', 'start'), ['work'])
        self.assertEqual(reserve_selection(BUNDLE, 'telefono de las oficinas de Rio Grande', 'start'), ['contacts'])

    def test_nothing_is_guessed_without_a_clear_match(self):
        for question in ('hola', 'consulta', 'cuánto sale el dólar', 'quiero hablar con una persona', ''):
            self.assertEqual(reserve_selection(BUNDLE, question, 'start'), [], question)

    def test_the_start_menu_is_never_the_answer(self):
        self.assertNotIn('start', reserve_selection(BUNDLE, 'certificado cud trabajo contactos', 'start'))

    def test_select_nodes_uses_it_only_when_the_interpreter_fails(self):
        nodes = select_nodes(BUNDLE, 'requisitos del CUD', 'start', _down)
        self.assertEqual(nodes[0]['id'], 'cud')
        self.assertEqual(select_nodes(BUNDLE, 'requisitos del CUD', 'start', lambda *_: {'node_ids': []}), [])
        with self.assertRaises(ContentError) as failure:
            select_nodes(BUNDLE, 'cuánto sale el dólar', 'start', _down)
        self.assertEqual(failure.exception.code, 'knowledge_interpretation_unavailable')


if __name__ == '__main__':
    unittest.main()
