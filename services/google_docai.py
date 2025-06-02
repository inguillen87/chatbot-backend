# services/google_docai.py
# ... (otros imports y la carga de credenciales como están) ...

def _obtener_documento_ai(pdf_path: str) -> Optional[documentai.Document]:
    if not CREDENTIALS_LOADED_SUCCESSFULLY or not GOOGLE_CREDENTIALS:
        logger.error("Imposible procesar PDF: Credenciales Google no están cargadas o son inválidas.")
        return None
    try:
        project_id = os.getenv("GOOGLE_PROJECT_ID")
        location = os.getenv("GOOGLE_DOCAI_LOCATION", "us") 
        processor_id = os.getenv("GOOGLE_DOCAI_PROCESSOR_ID")

        if not all([project_id, location, processor_id]):
            missing = [v_name for v_name,val in [("GOOGLE_PROJECT_ID",project_id),("GOOGLE_DOCAI_LOCATION",location),("GOOGLE_DOCAI_PROCESSOR_ID",processor_id)] if not val]
            logger.error(f"❌ Faltan variables de entorno Google DocAI: {', '.join(missing)}.")
            return None
            
        client_options = {"api_endpoint": f"{location}-documentai.googleapis.com"}
        client = documentai.DocumentProcessorServiceClient(credentials=GOOGLE_CREDENTIALS, client_options=client_options)
        resource_name = client.processor_path(project_id, location, processor_id)

        with open(pdf_path, "rb") as file:
            pdf_content = file.read()

        raw_document_proto = documentai.RawDocument(content=pdf_content, mime_type="application/pdf")
        
        # --- CORRECCIÓN AQUÍ ---
        # Vamos a usar una llamada más simple, confiando en la configuración 
        # de tu procesador en Google Cloud para la extracción de tablas.
        # Si aún necesitas forzar OCR o algo específico, consulta la documentación
        # de la versión EXACTA de tu librería google-cloud-documentai.
        request_doc_ai = documentai.ProcessRequest(
            name=resource_name, 
            raw_document=raw_document_proto, 
            skip_human_review=True
        )
        # --- FIN CORRECCIÓN ---

        logger.info(f"Enviando '{os.path.basename(pdf_path)}' a Document AI (Processor: {processor_id})...")
        result = client.process_document(request=request_doc_ai)
        
        if result and result.document:
            # El log original aquí causaba el error si result.document.tables no existía.
            # Lo hacemos más seguro:
            num_tablas = 0
            if hasattr(result.document, 'tables') and result.document.tables is not None:
                num_tablas = len(result.document.tables)
            
            logger.info(f"Documento procesado. Texto (parcial): '{result.document.text[:100] if result.document.text else 'N/A'}'. Tablas encontradas: {num_tablas}")
            if not result.document.text and num_tablas == 0:
                 logger.warning(f"DocAI procesó '{os.path.basename(pdf_path)}', pero no extrajo texto ni tablas.")
                 # Devolver el documento igual por si la lógica de tablas puede hacer algo,
                 # o None si prefieres que falle aquí. Por ahora, devolvemos el documento.
            return result.document
        else:
            logger.error(f"Document AI no devolvió un resultado de documento válido para '{os.path.basename(pdf_path)}'.")
            return None
    except Exception as e:
        logger.error(f"❌ Error en llamada a API Google Document AI para '{os.path.basename(pdf_path)}': {e}", exc_info=True)
        return None

# ... (El resto de google_docai.py: procesar_tablas_document_ai, extraer_info_producto_de_linea_pdf, 
#      y procesar_catalogo_pdf_google se mantienen como en la versión que te pasé en @‶gANVneHZLGZv... 
#      Recuerda que procesar_tablas_document_ai necesita TU personalización del HEADER_MAP)