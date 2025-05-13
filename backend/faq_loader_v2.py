from app import create_app, db
from models import QA, User
from werkzeug.security import generate_password_hash

faq_data = [
    # Envíos
    {"question": "¿Cuánto cuesta el envío?", "keywords": "envio,costo,precio", "answer": "El envío estándar cuesta $500, gratis en pedidos mayores a $10,000."},
    {"question": "¿Cuánto tarda el envío?", "keywords": "demora,tiempo,envio,tarda", "answer": "La entrega demora entre 2 y 5 días hábiles."},
    {"question": "¿Hacen envíos internacionales?", "keywords": "internacional,pais,exterior", "answer": "Sí, realizamos envíos internacionales vía DHL."},
    {"question": "¿Puedo retirar en el local?", "keywords": "retiro,local,presencial", "answer": "Sí, podés retirar en nuestro local."},
    {"question": "¿Tienen envío express?", "keywords": "envio,urgente,express", "answer": "Sí, contamos con envío express en 24 hs."},
    {"question": "¿Qué empresas hacen sus envíos?", "keywords": "empresa,logistica,courier", "answer": "Trabajamos con Andreani, OCA y DHL."},
    {"question": "¿Cómo hago seguimiento del envío?", "keywords": "seguimiento,tracking,envio", "answer": "Te enviaremos un código para hacer el seguimiento online."},

    # Pagos
    {"question": "¿Qué métodos de pago aceptan?", "keywords": "pago,tarjeta,transferencia", "answer": "Aceptamos tarjeta, transferencia y MercadoPago."},
    {"question": "¿Aceptan cuotas sin interés?", "keywords": "cuotas,sin interes,financiacion", "answer": "Sí, tenemos hasta 6 cuotas sin interés."},
    {"question": "¿Puedo pagar al recibir?", "keywords": "contraentrega,pago,recibir", "answer": "No, actualmente no aceptamos pagos contraentrega."},
    {"question": "¿Es seguro pagar en su web?", "keywords": "seguro,pago,web,proteccion", "answer": "Sí, utilizamos tecnología SSL y MercadoPago para máxima seguridad."},
    {"question": "¿Qué tarjetas aceptan?", "keywords": "tarjetas,credito,debito", "answer": "Visa, MasterCard, American Express, Cabal."},

    # Productos y stock
    {"question": "¿Tienen stock disponible?", "keywords": "stock,disponible", "answer": "Sí, el stock mostrado en la web está actualizado."},
    {"question": "¿Cuándo reponen stock?", "keywords": "reponer,reposición", "answer": "Reponemos stock cada semana."},
    {"question": "¿Hacen productos personalizados?", "keywords": "personalizados,a medida", "answer": "Sí, consultanos por WhatsApp para productos personalizados."},

    # Cambios y devoluciones
    {"question": "¿Cómo realizo una devolución?", "keywords": "devolucion,cambio", "answer": "Escribinos por WhatsApp con tu número de pedido."},
    {"question": "¿Qué garantía tienen los productos?", "keywords": "garantia,productos", "answer": "Todos nuestros productos tienen 6 meses de garantía."},
    {"question": "¿Aceptan cambios?", "keywords": "cambio,producto", "answer": "Sí, dentro de los primeros 30 días."},

    # Atención al cliente
    {"question": "¿Cuál es su horario de atención?", "keywords": "horario,atencion", "answer": "De lunes a viernes, 9 a 18 hs."},
    {"question": "¿Dónde están ubicados?", "keywords": "direccion,ubicacion", "answer": "Estamos en Mendoza, Argentina."},
    {"question": "¿Puedo contactarlos por teléfono?", "keywords": "telefono,contacto,llamar", "answer": "Sí, al 2613168608."},

    # Mayoristas y descuentos
    {"question": "¿Ofrecen precios mayoristas?", "keywords": "mayorista,precio", "answer": "Sí, escribinos para conocer condiciones mayoristas."},
    {"question": "¿Tienen descuentos por cantidad?", "keywords": "descuentos,cantidad", "answer": "Sí, ofrecemos descuentos por compras en volumen."},
    {"question": "¿Hay promociones vigentes?", "keywords": "promociones,ofertas", "answer": "Sí, consultá la sección Promociones en nuestra web."},

    # Facturación
    {"question": "¿Hacen factura A?", "keywords": "factura,A", "answer": "Sí, realizamos factura A."},
    {"question": "¿Cómo solicito mi factura?", "keywords": "solicitar,factura", "answer": "Solicitala al realizar el pedido o por WhatsApp."},

    # Tienda online
    {"question": "¿Cómo compro en la web?", "keywords": "comprar,web", "answer": "Agregá productos al carrito y seguí los pasos de pago."},
    {"question": "¿Debo registrarme para comprar?", "keywords": "registro,comprar", "answer": "No es necesario, podés comprar como invitado."},
    {"question": "¿Cómo creo una cuenta?", "keywords": "crear,cuenta,registro", "answer": "En la opción Mi Cuenta, registrate fácilmente."},

    # Servicios adicionales
    {"question": "¿Ofrecen envoltorio de regalo?", "keywords": "regalo,envoltorio", "answer": "Sí, solicitá envoltorio especial al hacer tu pedido."},
    {"question": "¿Los productos vienen con manual?", "keywords": "manual,instrucciones", "answer": "Sí, todos incluyen manual detallado."},

    # Problemas frecuentes
    {"question": "No recibí mi pedido, ¿qué hago?", "keywords": "pedido,no recibido", "answer": "Contactanos por WhatsApp para solucionarlo."},
    {"question": "El producto llegó dañado, ¿qué hago?", "keywords": "dañado,roto", "answer": "Comunicate inmediatamente con nosotros por WhatsApp."},

    # Redes sociales
    {"question": "¿Están en Instagram?", "keywords": "instagram,redes", "answer": "Sí, seguinos en @mybstore.ar."},
    {"question": "¿Tienen Facebook?", "keywords": "facebook,redes", "answer": "Sí, buscá nuestra página en Facebook como 'mybstore'."},

    # Temas legales
    {"question": "¿Cuáles son sus términos y condiciones?", "keywords": "terminos,condiciones", "answer": "Encontralos en el pie de nuestra web."},
    {"question": "¿Qué hacen con mis datos personales?", "keywords": "datos personales,privacidad", "answer": "Tus datos se protegen según nuestra política de privacidad disponible en la web."},

    # Otras consultas generales
    {"question": "¿Tienen atención por chat online?", "keywords": "chat online,atencion", "answer": "Sí, disponible en la web."},
    {"question": "¿Ofrecen soporte postventa?", "keywords": "soporte,postventa", "answer": "Sí, brindamos soporte completo postventa."},
    {"question": "¿Puedo cancelar un pedido?", "keywords": "cancelar,pedido", "answer": "Podés cancelar dentro de las primeras 24 hs del pedido."},
    {"question": "¿Cómo modifico mi pedido?", "keywords": "modificar,pedido", "answer": "Escribinos rápidamente por WhatsApp."},
    {"question": "¿Ofrecen servicios personalizados para empresas?", "keywords": "empresas,servicios", "answer": "Sí, ofrecemos servicios personalizados para empresas."},
]

users_data = [
    {
        "name": "Marcelo",
        "email": "marcelo@marcelo.com",
        "password": "1234",
        "token": "token-marcelo"
    },
    {
        "name": "Admin",
        "email": "admin@admin.com",
        "password": "admin",
        "token": "token-admin"
    }
]

app = create_app()

with app.app_context():
    db.drop_all()
    db.create_all()

    for item in faq_data:
        qa = QA(question=item['question'], keywords=item['keywords'], answer=item['answer'])
        db.session.add(qa)

    for user in users_data:
        hashed_pw = generate_password_hash(user['password'])
        print(f"🔐 Hash generado para {user['email']}: {hashed_pw}")
        u = User(
            name=user['name'],
            email=user['email'],
            password_hash=hashed_pw,
            token=user['token']
        )
        db.session.add(u)

    db.session.commit()
    print("✅ FAQs y usuarios cargados correctamente.")