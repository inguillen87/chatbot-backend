import unittest

from services.municipio_responder import decide_modalidad


class ModalidadRouterTests(unittest.TestCase):
    def test_image_routes_to_cv(self):
        modalidad, accion = decide_modalidad({'media_content_type': 'image/jpeg'})
        self.assertEqual(modalidad, 'image')
        self.assertEqual(accion, 'cv')

    def test_video_routes_to_cv(self):
        modalidad, accion = decide_modalidad({'media_content_type': 'video/mp4'})
        self.assertEqual(modalidad, 'video')
        self.assertEqual(accion, 'cv')

    def test_location_routes_to_confirmation(self):
        payload = {'location': {'latitude': 1.0, 'longitude': 2.0}}
        modalidad, accion = decide_modalidad(payload)
        self.assertEqual(modalidad, 'location')
        self.assertEqual(accion, 'confirm_location')

    def test_voice_routes_to_asr(self):
        modalidad, accion = decide_modalidad({'media_content_type': 'audio/ogg'})
        self.assertEqual(modalidad, 'voice')
        self.assertEqual(accion, 'asr')

    def test_text_default(self):
        modalidad, accion = decide_modalidad('hola que tal')
        self.assertEqual(modalidad, 'text')
        self.assertEqual(accion, 'text')


if __name__ == '__main__':
    unittest.main()
