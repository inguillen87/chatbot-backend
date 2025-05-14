from models import QA, Rubro
from extensions import db

def cargar_faqs_iniciales():
    if QA.query.count() == 0:
        print("\n🚀 Cargando FAQs base por rubro...")

        rubros = Rubro.query.all()
        rubros = {r.clave: r for r in rubros}  # Asegura que el loader use los rubros ya creados

        faqs = [

            # BODEGAS
            QA(question="¿Qué tipos de vinos ofrecen?", answer="Tenemos vinos tintos, blancos, rosados y espumantes.", rubro=rubros["bodega"]),
            QA(question="¿Tienen tienda online?", answer="Sí, podés comprar directamente desde nuestra web.", rubro=rubros["bodega"]),
            QA(question="¿Hacen envíos a todo el país?", answer="Sí, realizamos envíos a domicilio en toda Argentina.", rubro=rubros["bodega"]),
            QA(question="¿Puedo visitar la bodega?", answer="Sí, recibimos visitas con reserva previa.", rubro=rubros["bodega"]),
            QA(question="¿Tienen visitas guiadas?", answer="Sí, ofrecemos recorridos con degustaciones.", rubro=rubros["bodega"]),
            QA(question="¿Hacen eventos o celebraciones?", answer="Sí, podés reservar para celebraciones especiales.", rubro=rubros["bodega"]),
            QA(question="¿Qué promociones tienen?", answer="Tenemos packs especiales y descuentos por volumen.", rubro=rubros["bodega"]),
            QA(question="¿Tienen vinos orgánicos?", answer="Sí, contamos con una línea de vinos orgánicos certificados.", rubro=rubros["bodega"]),
            QA(question="¿Puedo regalar vinos?", answer="Sí, ofrecemos opciones de regalo con envío incluido.", rubro=rubros["bodega"]),
            QA(question="¿Se puede pagar en cuotas?", answer="Sí, aceptamos cuotas con tarjetas bancarias.", rubro=rubros["bodega"]),

            # RESTAURANTES
            QA(question="¿Tienen menú del día?", answer="Sí, ofrecemos opciones diarias con bebida incluida.", rubro=rubros["restaurante"]),
            QA(question="¿Cómo reservo una mesa?", answer="Podés hacerlo por WhatsApp o desde nuestra web.", rubro=rubros["restaurante"]),
            QA(question="¿Tienen opciones vegetarianas?", answer="Sí, contamos con menús vegetarianos y veganos.", rubro=rubros["restaurante"]),
            QA(question="¿Hacen envíos a domicilio?", answer="Sí, trabajamos con apps y delivery propio.", rubro=rubros["restaurante"]),
            QA(question="¿Aceptan tarjetas de débito y crédito?", answer="Sí, aceptamos todos los medios de pago.", rubro=rubros["restaurante"]),
            QA(question="¿Tienen promociones?", answer="Sí, ofrecemos promociones de lunes a viernes al mediodía.", rubro=rubros["restaurante"]),
            QA(question="¿Puedo pedir comida para llevar?", answer="Sí, preparaciones para llevar disponibles en caja.", rubro=rubros["restaurante"]),
            QA(question="¿Tienen menú para celíacos?", answer="Sí, ofrecemos opciones sin TACC certificadas.", rubro=rubros["restaurante"]),
            QA(question="¿Cuántas personas pueden reservar por mesa?", answer="Hasta 6 personas por reserva online.", rubro=rubros["restaurante"]),
            QA(question="¿Abren los domingos?", answer="Sí, abrimos todos los días salvo lunes.", rubro=rubros["restaurante"]),

            # HELADERÍAS
            QA(question="¿Qué sabores tienen hoy?", answer="Contamos con más de 20 sabores disponibles todos los días.", rubro=rubros["heladeria"]),
            QA(question="¿Hacen envíos a domicilio?", answer="Sí, podés pedir por delivery o apps.", rubro=rubros["heladeria"]),
            QA(question="¿Tienen sabores sin azúcar?", answer="Sí, ofrecemos opciones aptas para diabéticos.", rubro=rubros["heladeria"]),
            QA(question="¿Tienen promociones por kilo?", answer="Sí, promociones de lunes a jueves en sabores seleccionados.", rubro=rubros["heladeria"]),
            QA(question="¿Venden cucuruchos y potes aparte?", answer="Sí, podés llevar extras o pedir solo los envases.", rubro=rubros["heladeria"]),
            QA(question="¿Puedo pagar con QR?", answer="Sí, aceptamos QR, MercadoPago y tarjetas.", rubro=rubros["heladeria"]),
            QA(question="¿Tienen café o postres?", answer="Sí, contamos con cafetería y productos dulces.", rubro=rubros["heladeria"]),
            QA(question="¿Abren los feriados?", answer="Sí, excepto feriados nacionales mayores.", rubro=rubros["heladeria"]),
            QA(question="¿Hay sillas para comer en el lugar?", answer="Sí, tenemos mesas internas y al aire libre.", rubro=rubros["heladeria"]),
            QA(question="¿Hacen tortas heladas?", answer="Sí, podés encargarlas con anticipación.", rubro=rubros["heladeria"]),
            
            # PELUQUERIAS
            QA(question="¿Cómo saco un turno?", answer="Podés agendar turno por WhatsApp o desde nuestro sistema online.", rubro=rubros["peluqueria"]),
            QA(question="¿Atienden sin turno?", answer="Recomendamos reservar turno, pero también atendemos por orden de llegada según disponibilidad.", rubro=rubros["peluqueria"]),
            QA(question="¿Hacen cortes para niños?", answer="Sí, atendemos a todas las edades.", rubro=rubros["peluqueria"]),
            QA(question="¿Hacen color o reflejos?", answer="Sí, realizamos coloraciones, reflejos y tratamientos capilares.", rubro=rubros["peluqueria"]),
            QA(question="¿Cuentan con barbero?", answer="Sí, tenemos servicio completo de barbería.", rubro=rubros["peluqueria"]),
            QA(question="¿Qué medios de pago aceptan?", answer="Efectivo, tarjetas, QR y transferencias.", rubro=rubros["peluqueria"]),
            QA(question="¿Tienen promociones?", answer="Sí, descuentos por combos de servicios o días especiales.", rubro=rubros["peluqueria"]),
            QA(question="¿Realizan alisados o tratamientos especiales?", answer="Sí, hacemos botox capilar, alisados, keratina, etc.", rubro=rubros["peluqueria"]),
            QA(question="¿Se puede regalar un servicio?", answer="Sí, tenemos vouchers de regalo.", rubro=rubros["peluqueria"]),
            QA(question="¿Cuál es el horario de atención?", answer="De lunes a sábados, de 10 a 20 hs.", rubro=rubros["peluqueria"]),

            # SERVICIO TECNICO
            QA(question="¿Qué tipo de equipos reparan?", answer="Celulares, notebooks, PCs, tablets y electrodomésticos pequeños.", rubro=rubros["servicio_tecnico"]),
            QA(question="¿Tienen diagnóstico sin cargo?", answer="Sí, ofrecemos diagnóstico gratuito en el local.", rubro=rubros["servicio_tecnico"]),
            QA(question="¿Cuánto demora una reparación?", answer="Depende del caso, pero la mayoría se resuelven entre 24 y 72 hs.", rubro=rubros["servicio_tecnico"]),
            QA(question="¿Qué garantía ofrecen?", answer="Garantía de 30 a 90 días según el tipo de reparación.", rubro=rubros["servicio_tecnico"]),
            QA(question="¿Venden repuestos?", answer="Sí, vendemos algunos repuestos comunes.", rubro=rubros["servicio_tecnico"]),
            QA(question="¿Hacen servicio a domicilio?", answer="Sí, con turno previo y dependiendo de la zona.", rubro=rubros["servicio_tecnico"]),
            QA(question="¿Reparan placas madre o micro soldaduras?", answer="Sí, tenemos servicio de microelectrónica avanzada.", rubro=rubros["servicio_tecnico"]),
            QA(question="¿Puedo pagar con tarjeta o QR?", answer="Sí, aceptamos pagos digitales, tarjetas y efectivo.", rubro=rubros["servicio_tecnico"]),
            QA(question="¿Tienen atención los sábados?", answer="Sí, de 9 a 13 hs.", rubro=rubros["servicio_tecnico"]),
            QA(question="¿Qué pasa si no se puede reparar?", answer="No cobramos si no podemos resolver el problema.", rubro=rubros["servicio_tecnico"]),
            
            # MÉDICOS GENERALES
            QA(question="¿Cómo saco un turno?", answer="Podés solicitar un turno llamando al consultorio o por WhatsApp.", rubro=rubros["medico"]),
            QA(question="¿Atienden con obra social?", answer="Sí, trabajamos con las principales obras sociales y prepagas.", rubro=rubros["medico"]),
            QA(question="¿Atienden urgencias?", answer="Solo trabajamos con turnos programados. No realizamos guardias.", rubro=rubros["medico"]),
            QA(question="¿Tienen atención pediátrica?", answer="Sí, contamos con especialistas en atención infantil.", rubro=rubros["medico"]),
            QA(question="¿Qué especialidades médicas ofrecen?", answer="Clínica médica, pediatría, ginecología, dermatología y más.", rubro=rubros["medico"]),
            QA(question="¿Hacen certificados médicos?", answer="Sí, siempre que la consulta lo justifique.", rubro=rubros["medico"]),
            QA(question="¿Cuánto dura la consulta?", answer="En promedio entre 20 y 30 minutos.", rubro=rubros["medico"]),
            QA(question="¿Se puede pagar con tarjeta?", answer="Sí, aceptamos débito, crédito y transferencias.", rubro=rubros["medico"]),
            QA(question="¿Puedo cancelar mi turno?", answer="Sí, con al menos 24 horas de anticipación.", rubro=rubros["medico"]),
            QA(question="¿Tienen consultorio accesible?", answer="Sí, con rampa de ingreso y baño adaptado.", rubro=rubros["medico"]),

            # DENTISTAS
            QA(question="¿Atienden urgencias dentales?", answer="Sí, contamos con turnos de urgencia para casos críticos.", rubro=rubros["dentista"]),
            QA(question="¿Cómo saco un turno con el odontólogo?", answer="Podés reservar tu turno por WhatsApp o llamando al consultorio.", rubro=rubros["dentista"]),
            QA(question="¿Realizan limpiezas o blanqueamientos?", answer="Sí, realizamos ambos tratamientos previa consulta.", rubro=rubros["dentista"]),
            QA(question="¿Hacen ortodoncia o brackets?", answer="Sí, contamos con especialistas en ortodoncia.", rubro=rubros["dentista"]),
            QA(question="¿Atienden niños?", answer="Sí, ofrecemos odontopediatría.", rubro=rubros["dentista"]),
            QA(question="¿Puedo pagar en cuotas?", answer="Sí, tenemos planes de pago para tratamientos largos.", rubro=rubros["dentista"]),
            QA(question="¿Hacen radiografías dentales?", answer="Sí, tenemos servicio de radiología dentro del consultorio.", rubro=rubros["dentista"]),
            QA(question="¿Qué obras sociales aceptan?", answer="Podés consultarnos la lista actualizada por WhatsApp.", rubro=rubros["dentista"]),
            QA(question="¿Hacen implantes dentales?", answer="Sí, con diagnóstico previo y presupuesto personalizado.", rubro=rubros["dentista"]),
            QA(question="¿Qué medidas de higiene aplican?", answer="Cumplimos todos los protocolos sanitarios exigidos.", rubro=rubros["dentista"]),

            # KINESIÓLOGOS
            QA(question="¿Qué tipo de rehabilitación hacen?", answer="Traumatológica, postquirúrgica y deportiva.", rubro=rubros["kinesiologo"]),
            QA(question="¿Atienden por derivación médica?", answer="Sí, aceptamos derivaciones de médicos clínicos o traumatólogos.", rubro=rubros["kinesiologo"]),
            QA(question="¿Puedo venir sin orden médica?", answer="Sí, pero algunas obras sociales la exigen.", rubro=rubros["kinesiologo"]),
            QA(question="¿Qué duración tienen las sesiones?", answer="Aproximadamente 45 minutos por turno.", rubro=rubros["kinesiologo"]),
            QA(question="¿Qué ejercicios hacen?", answer="Fortalecimiento, elongación, movilidad articular, etc.", rubro=rubros["kinesiologo"]),
            QA(question="¿Qué equipos utilizan?", answer="Ultrasonido, magnetoterapia, electroestimulación, entre otros.", rubro=rubros["kinesiologo"]),
            QA(question="¿Atienden lesiones deportivas?", answer="Sí, tenemos experiencia en recuperación funcional.", rubro=rubros["kinesiologo"]),
            QA(question="¿Tienen turnos los sábados?", answer="Sí, bajo disponibilidad previa.", rubro=rubros["kinesiologo"]),
            QA(question="¿Cuántas sesiones incluye el tratamiento?", answer="Depende de la patología y evolución del paciente.", rubro=rubros["kinesiologo"]),
            QA(question="¿Dónde están ubicados?", answer="Estamos en el centro, a 2 cuadras de la plaza principal.", rubro=rubros["kinesiologo"]),

            # NUTRICIONISTAS
            QA(question="¿Hacen planes personalizados?", answer="Sí, adaptados a cada paciente según su objetivo y antecedentes.", rubro=rubros["nutricionista"]),
            QA(question="¿Atienden sobrepeso o diabetes?", answer="Sí, tratamos múltiples condiciones metabólicas y alimentarias.", rubro=rubros["nutricionista"]),
            QA(question="¿Hacen asesoramiento deportivo?", answer="Sí, especialmente para rendimiento físico y fuerza.", rubro=rubros["nutricionista"]),
            QA(question="¿Atienden niños?", answer="Sí, con enfoque pediátrico y educativo.", rubro=rubros["nutricionista"]),
            QA(question="¿Tienen consulta online?", answer="Sí, ofrecemos atención por videollamada.", rubro=rubros["nutricionista"]),
            QA(question="¿Qué incluye la primera consulta?", answer="Evaluación, medición antropométrica y entrega de guía inicial.", rubro=rubros["nutricionista"]),
            QA(question="¿Puedo pagar con QR?", answer="Sí, aceptamos todos los medios digitales.", rubro=rubros["nutricionista"]),
            QA(question="¿Cuántas consultas necesito?", answer="Depende del objetivo, generalmente entre 2 y 4 al mes.", rubro=rubros["nutricionista"]),
            QA(question="¿Trabajan con psicólogos o médicos?", answer="Sí, hacemos seguimiento interdisciplinario cuando se requiere.", rubro=rubros["nutricionista"]),
            QA(question="¿Puedo hacer consultas por WhatsApp?", answer="Sí, para seguimiento o dudas entre sesiones.", rubro=rubros["nutricionista"]),

            # OFTALMÓLOGOS
            QA(question="¿Hacen exámenes visuales completos?", answer="Sí, revisamos agudeza, fondo de ojo y presión ocular.", rubro=rubros["oftalmologo"]),
            QA(question="¿Recetan anteojos?", answer="Sí, emitimos receta óptica válida para cualquier óptica.", rubro=rubros["oftalmologo"]),
            QA(question="¿Atienden niños?", answer="Sí, hacemos controles visuales desde los 5 años.", rubro=rubros["oftalmologo"]),
            QA(question="¿Hacen estudios para licencias de conducir?", answer="Sí, emitimos certificados visuales oficiales.", rubro=rubros["oftalmologo"]),
            QA(question="¿Atienden urgencias oftalmológicas?", answer="Sí, conjuntivitis, cuerpos extraños o visión borrosa.", rubro=rubros["oftalmologo"]),
            QA(question="¿Qué tipo de patologías tratan?", answer="Miopía, astigmatismo, glaucoma, cataratas, etc.", rubro=rubros["oftalmologo"]),
            QA(question="¿Puedo hacerme un fondo de ojo?", answer="Sí, con turno programado y dilatación ocular.", rubro=rubros["oftalmologo"]),
            QA(question="¿Venden anteojos?", answer="No directamente, pero derivamos a ópticas de confianza.", rubro=rubros["oftalmologo"]),
            QA(question="¿Atienden obras sociales?", answer="Sí, con credencial y orden correspondiente.", rubro=rubros["oftalmologo"]),
            QA(question="¿Cómo saco turno?", answer="Por WhatsApp o a través de la web de turnos.", rubro=rubros["oftalmologo"]),
                              
        ]

    # Insertar sin duplicados (por pregunta y rubro)
    nuevas_faqs = []
    for faq in faqs:
        existe = QA.query.filter_by(question=faq.question, rubro_id=faq.rubro.id).first()
        if not existe:
            nuevas_faqs.append(faq)

    if nuevas_faqs:
        db.session.bulk_save_objects(nuevas_faqs)
        db.session.commit()
        print(f"✅ Se cargaron {len(nuevas_faqs)} nuevas preguntas frecuentes.\n")
    else:
        print("📚 Todas las preguntas ya estaban cargadas. No se insertaron duplicados.\n")

        db.session.bulk_save_objects(faqs)
        db.session.commit()
        print("✅ Preguntas frecuentes cargadas con éxito.\n")
    else:
        print("📚 La base ya contiene FAQs. No se cargaron duplicados.\n")







            # PELUQUERÍAS Y BARBERÍAS
            QA(question="¿Cómo saco un turno?", answer="Podés agendar turno por WhatsApp o desde nuestro sistema online.", rubro=rubros["peluqueria"]),
            QA(question="¿Atienden sin turno?", answer="Recomendamos reservar turno, pero también atendemos por orden de llegada según disponibilidad.", rubro=rubros["peluqueria"]),
            QA(question="¿Hacen cortes para niños?", answer="Sí, atendemos a todas las edades.", rubro=rubros["peluqueria"]),
            QA(question="¿Hacen color o reflejos?", answer="Sí, realizamos coloraciones, reflejos y tratamientos capilares.", rubro=rubros["peluqueria"]),
            QA(question="¿Cuentan con barbero?", answer="Sí, tenemos servicio completo de barbería.", rubro=rubros["peluqueria"]),
            QA(question="¿Qué medios de pago aceptan?", answer="Efectivo, tarjetas, QR y transferencias.", rubro=rubros["peluqueria"]),
            QA(question="¿Tienen promociones?", answer="Sí, descuentos por combos de servicios o días especiales.", rubro=rubros["peluqueria"]),
            QA(question="¿Realizan alisados o tratamientos especiales?", answer="Sí, hacemos botox capilar, alisados, keratina, etc.", rubro=rubros["peluqueria"]),
            QA(question="¿Se puede regalar un servicio?", answer="Sí, tenemos vouchers de regalo.", rubro=rubros["peluqueria"]),
            QA(question="¿Cuál es el horario de atención?", answer="De lunes a sábados, de 10 a 20 hs.", rubro=rubros["peluqueria"]),

            # SERVICIO TÉCNICO
            QA(question="¿Qué tipo de equipos reparan?", answer="Celulares, notebooks, PCs, tablets y electrodomésticos pequeños.", rubro=rubros["servicio_tecnico"]),
            QA(question="¿Tienen diagnóstico sin cargo?", answer="Sí, ofrecemos diagnóstico gratuito en el local.", rubro=rubros["servicio_tecnico"]),
            QA(question="¿Cuánto demora una reparación?", answer="Depende del caso, pero la mayoría se resuelven entre 24 y 72 hs.", rubro=rubros["servicio_tecnico"]),
            QA(question="¿Qué garantía ofrecen?", answer="Garantía de 30 a 90 días según el tipo de reparación.", rubro=rubros["servicio_tecnico"]),
            QA(question="¿Venden repuestos?", answer="Sí, vendemos algunos repuestos comunes.", rubro=rubros["servicio_tecnico"]),
            QA(question="¿Hacen servicio a domicilio?", answer="Sí, con turno previo y dependiendo de la zona.", rubro=rubros["servicio_tecnico"]),
            QA(question="¿Reparan placas madre o micro soldaduras?", answer="Sí, tenemos servicio de microelectrónica avanzada.", rubro=rubros["servicio_tecnico"]),
            QA(question="¿Puedo pagar con tarjeta o QR?", answer="Sí, aceptamos pagos digitales, tarjetas y efectivo.", rubro=rubros["servicio_tecnico"]),
            QA(question="¿Tienen atención los sábados?", answer="Sí, de 9 a 13 hs.", rubro=rubros["servicio_tecnico"]),
            QA(question="¿Qué pasa si no se puede reparar?", answer="No cobramos si no podemos resolver el problema.", rubro=rubros["servicio_tecnico"]),

            # BODEGAS
            QA(question="¿Qué tipos de vinos ofrecen?", answer="Tenemos vinos tintos, blancos, rosados y espumantes.", rubro=rubros["bodega"]),
            QA(question="¿Tienen tienda online?", answer="Sí, podés comprar directamente desde nuestra web.", rubro=rubros["bodega"]),
            QA(question="¿Hacen envíos a todo el país?", answer="Sí, realizamos envíos a domicilio en toda Argentina.", rubro=rubros["bodega"]),
            QA(question="¿Puedo visitar la bodega?", answer="Sí, recibimos visitas con reserva previa.", rubro=rubros["bodega"]),
            QA(question="¿Tienen visitas guiadas?", answer="Sí, ofrecemos recorridos con degustaciones.", rubro=rubros["bodega"]),
            QA(question="¿Hacen eventos o celebraciones?", answer="Sí, podés reservar para celebraciones especiales.", rubro=rubros["bodega"]),
            QA(question="¿Qué promociones tienen?", answer="Tenemos packs especiales y descuentos por volumen.", rubro=rubros["bodega"]),
            QA(question="¿Tienen vinos orgánicos?", answer="Sí, contamos con una línea de vinos orgánicos certificados.", rubro=rubros["bodega"]),
            QA(question="¿Puedo regalar vinos?", answer="Sí, ofrecemos opciones de regalo con envío incluido.", rubro=rubros["bodega"]),
            QA(question="¿Se puede pagar en cuotas?", answer="Sí, aceptamos cuotas con tarjetas bancarias.", rubro=rubros["bodega"]),
            
            # RESTAURANTES
            QA(question="¿Tienen menú del día?", answer="Sí, ofrecemos opciones diarias con bebida incluida.", rubro=rubros["restaurante"]),
            QA(question="¿Cómo reservo una mesa?", answer="Podés hacerlo por WhatsApp o desde nuestra web.", rubro=rubros["restaurante"]),
            QA(question="¿Tienen opciones vegetarianas?", answer="Sí, contamos con menús vegetarianos y veganos.", rubro=rubros["restaurante"]),
            QA(question="¿Hacen envíos a domicilio?", answer="Sí, trabajamos con apps y delivery propio.", rubro=rubros["restaurante"]),
            QA(question="¿Aceptan tarjetas de débito y crédito?", answer="Sí, aceptamos todos los medios de pago.", rubro=rubros["restaurante"]),
            QA(question="¿Tienen promociones?", answer="Sí, ofrecemos promociones de lunes a viernes al mediodía.", rubro=rubros["restaurante"]),
            QA(question="¿Puedo pedir comida para llevar?", answer="Sí, preparaciones para llevar disponibles en caja.", rubro=rubros["restaurante"]),
            QA(question="¿Tienen menú para celíacos?", answer="Sí, ofrecemos opciones sin TACC certificadas.", rubro=rubros["restaurante"]),
            QA(question="¿Cuántas personas pueden reservar por mesa?", answer="Hasta 6 personas por reserva online.", rubro=rubros["restaurante"]),
            QA(question="¿Abren los domingos?", answer="Sí, abrimos todos los días salvo lunes.", rubro=rubros["restaurante"]),

            #HELADERIAS
            QA(question="¿Qué sabores tienen hoy?", answer="Contamos con más de 20 sabores disponibles todos los días.", rubro=rubros["heladeria"]),
            QA(question="¿Hacen envíos a domicilio?", answer="Sí, podés pedir por delivery o apps.", rubro=rubros["heladeria"]),
            QA(question="¿Tienen sabores sin azúcar?", answer="Sí, ofrecemos opciones aptas para diabéticos.", rubro=rubros["heladeria"]),
            QA(question="¿Tienen promociones por kilo?", answer="Sí, promociones de lunes a jueves en sabores seleccionados.", rubro=rubros["heladeria"]),
            QA(question="¿Venden cucuruchos y potes aparte?", answer="Sí, podés llevar extras o pedir solo los envases.", rubro=rubros["heladeria"]),
            QA(question="¿Puedo pagar con QR?", answer="Sí, aceptamos QR, MercadoPago y tarjetas.", rubro=rubros["heladeria"]),
            QA(question="¿Tienen café o postres?", answer="Sí, contamos con cafetería y productos dulces.", rubro=rubros["heladeria"]),
            QA(question="¿Abren los feriados?", answer="Sí, excepto feriados nacionales mayores.", rubro=rubros["heladeria"]),
            QA(question="¿Hay sillas para comer en el lugar?", answer="Sí, tenemos mesas internas y al aire libre.", rubro=rubros["heladeria"]),
            QA(question="¿Hacen tortas heladas?", answer="Sí, podés encargarlas con anticipación.", rubro=rubros["heladeria"]),


       ]


        db.session.bulk_save_objects(faqs)
        db.session.commit()
        print("✅ Preguntas frecuentes cargadas con éxito.\n")
