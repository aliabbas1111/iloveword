"use strict";

/* ------------------------------------------------------------------ *
 * Configuration
 * ------------------------------------------------------------------ */
// Same-origin by default (the API serves this file). Override for a split
// deployment: <body data-api="https://api.example.com">
const API = (document.body.dataset.api || "").replace(/\/$/, "");

/* ------------------------------------------------------------------ *
 * Tool registry — the single source of truth for the grid, the modal
 * and the request. Endpoint paths and field names match the API exactly.
 * ------------------------------------------------------------------ */
const DOCX = ".docx,.docm,.dotx";
const TOOLS = [
  { id: "merge-docx",         path: "/api/tools/merge-docx",        field: "files", multiple: true, accept: DOCX, icon: "fa-object-group",  tone: "red",
    options: [{ name: "page_break", type: "checkbox", value: true, label: "opt_page_break" }] },
  { id: "split-docx",         path: "/api/tools/split-docx",        accept: DOCX, icon: "fa-scissors", tone: "orange",
    options: [{ name: "mode", type: "select", value: "pages", label: "opt_split_mode",
                choices: [["pages", "opt_split_pages"], ["count", "opt_split_count"]] },
              { name: "every", type: "number", value: 5, min: 1, max: 500, label: "opt_every", showWhen: { mode: "count" } }] },
  { id: "compress-docx",      path: "/api/tools/compress-docx",     accept: DOCX, icon: "fa-compress", tone: "green",
    options: [{ name: "max_dimension", type: "number", value: 1600, min: 320, max: 4000, label: "opt_max_dim" },
              { name: "quality", type: "number", value: 78, min: 30, max: 95, label: "opt_quality" }] },
  { id: "word-to-pdf",        path: "/api/convert/word-to-pdf",     accept: DOCX, icon: "fa-file-pdf", tone: "blue" },
  { id: "pdf-to-docx",        path: "/api/convert/pdf-to-docx",     accept: ".pdf", icon: "fa-file-word", tone: "indigo",
    options: [{ name: "start_page", type: "number", value: 1, min: 1, label: "opt_start_page" },
              { name: "end_page", type: "number", value: 0, min: 0, label: "opt_end_page" }] },
  { id: "word-to-jpg",        path: "/api/convert/word-to-jpg",     accept: DOCX, icon: "fa-image", tone: "yellow",
    options: [{ name: "dpi", type: "number", value: 150, min: 72, max: 300, label: "opt_dpi" },
              { name: "image_format", type: "select", value: "jpg", label: "opt_format",
                choices: [["jpg", "JPG"], ["png", "PNG"]] }] },
  { id: "image-to-docx",      path: "/api/convert/image-to-docx",   accept: ".png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff", icon: "fa-wand-magic-sparkles", tone: "purple",
    options: [{ name: "languages", type: "text", value: "", placeholder: "ara+eng", label: "opt_ocr_langs" }] },
  { id: "rtl-fixer",          path: "/api/tools/rtl-fixer",         accept: DOCX, icon: "fa-align-right", tone: "teal",
    options: [{ name: "force_all", type: "checkbox", value: false, label: "opt_force_all" }] },
  { id: "extract-text",       path: "/api/tools/extract-text",      accept: DOCX, icon: "fa-file-lines", tone: "pink" },
  { id: "protect-word",       path: "/api/tools/protect-word",      accept: DOCX, icon: "fa-lock", tone: "gray",
    options: [{ name: "password", type: "password", value: "", required: true, label: "opt_password" }] },
  { id: "unlock-word",        path: "/api/tools/unlock-word",       accept: DOCX, icon: "fa-lock-open", tone: "cyan",
    options: [{ name: "password", type: "password", value: "", label: "opt_password_known" }] },
  { id: "watermark-word",     path: "/api/tools/watermark-word",    accept: DOCX, icon: "fa-stamp", tone: "amber",
    options: [{ name: "text", type: "text", value: "", required: true, maxlength: 60, label: "opt_wm_text" },
              { name: "color", type: "color", value: "#C0C0C0", label: "opt_wm_color" },
              { name: "opacity", type: "range", value: 0.5, min: 0.05, max: 1, step: 0.05, label: "opt_wm_opacity" }] },
  { id: "remove-blank-pages", path: "/api/tools/remove-blank-pages", accept: DOCX, icon: "fa-file-circle-minus", tone: "rose" },
  { id: "word-to-html",       path: "/api/convert/word-to-html",    accept: DOCX, icon: "fa-code", tone: "violet" },
  { id: "html-to-word",       path: "/api/convert/html-to-word",    accept: ".html,.htm", icon: "fa-globe", tone: "emerald" },
  { id: "extract-images",     path: "/api/tools/extract-images",    accept: DOCX, icon: "fa-images", tone: "sky" },
  { id: "find-replace",       path: "/api/tools/find-replace",      accept: DOCX, icon: "fa-right-left", tone: "fuchsia",
    options: [{ name: "search", type: "text", value: "", required: true, label: "opt_search" },
              { name: "replace", type: "text", value: "", label: "opt_replace" },
              { name: "match_case", type: "checkbox", value: true, label: "opt_match_case" },
              { name: "whole_word", type: "checkbox", value: false, label: "opt_whole_word" }] },
  { id: "word-stats",         path: "/api/tools/word-stats",        accept: DOCX, icon: "fa-chart-pie", tone: "lime", json: true },
  { id: "markdown-to-word",   path: "/api/convert/markdown-to-word", accept: ".md,.markdown,.txt", icon: "fa-hashtag", tone: "stone" },
  { id: "clean-formatting",   path: "/api/tools/clean-formatting",  accept: DOCX, icon: "fa-broom", tone: "zinc",
    options: [{ name: "font_name", type: "text", value: "Calibri", label: "opt_font" },
              { name: "font_size", type: "number", value: 12, min: 6, max: 72, label: "opt_font_size" },
              { name: "keep_emphasis", type: "checkbox", value: true, label: "opt_keep_emphasis" }] },
  { id: "remove-metadata",    path: "/api/tools/remove-metadata",   accept: DOCX, icon: "fa-user-secret", tone: "slate" },
];

const TONES = {
  red:"bg-red-50 text-red-600", orange:"bg-orange-50 text-orange-600", green:"bg-green-50 text-green-600",
  blue:"bg-blue-50 text-blue-600", indigo:"bg-indigo-50 text-indigo-600", yellow:"bg-yellow-50 text-yellow-600",
  purple:"bg-purple-50 text-purple-600", teal:"bg-teal-50 text-teal-600", pink:"bg-pink-50 text-pink-600",
  gray:"bg-gray-100 text-gray-700", cyan:"bg-cyan-50 text-cyan-600", amber:"bg-amber-50 text-amber-600",
  rose:"bg-rose-50 text-rose-600", violet:"bg-violet-50 text-violet-600", emerald:"bg-emerald-50 text-emerald-600",
  sky:"bg-sky-50 text-sky-600", fuchsia:"bg-fuchsia-50 text-fuchsia-600", lime:"bg-lime-50 text-lime-600",
  stone:"bg-stone-100 text-stone-700", zinc:"bg-zinc-100 text-zinc-700", slate:"bg-slate-100 text-slate-700",
};

/* ------------------------------------------------------------------ *
 * i18n — keys are tool ids, so a card and its translation can never
 * drift apart (the old t1..t20 numbering silently mismatched).
 * ------------------------------------------------------------------ */
const I18N = {
  ar: { dir:"rtl",
    ui_title:"كل ما تحتاجه لإدارة ملفات Word في مكان واحد",
    ui_subtitle:"منصة متكاملة تضم 21 أداة لدمج المستندات وتحويلها وتعديلها باحترافية.",
    ui_search:"ابحث عن أداة…", ui_no_results:"لا توجد أداة مطابقة.",
    ui_footer:"جميع الحقوق محفوظة © 2026 — منصة ILoveWord",
    ui_drop:"اضغط لاختيار الملف أو اسحبه إلى هنا", ui_process:"ابدأ المعالجة",
    ui_processing:"جاري معالجة الملف…", ui_processing_hint:"قد تستغرق الملفات الكبيرة بضع ثوانٍ.",
    ui_done:"تمت المعالجة بنجاح", ui_download:"تنزيل الملف", ui_again:"معالجة ملف آخر",
    ui_retry:"إعادة المحاولة", ui_language:"اللغة", ui_remove:"إزالة",
    ui_network:"تعذّر الاتصال بالخادم. تأكد من تشغيل الواجهة الخلفية.",
    ui_need_file:"يرجى اختيار ملف أولاً.", ui_need_two:"يرجى اختيار ملفين على الأقل.",
    ui_required:"هذا الحقل مطلوب.", ui_accepts:"الصيغ المدعومة:",
    opt_page_break:"إدراج فاصل صفحات بين المستندات", opt_split_mode:"طريقة التقسيم",
    opt_split_pages:"عند فواصل الصفحات", opt_split_count:"كل عدد من الفقرات",
    opt_every:"عدد الفقرات لكل جزء", opt_max_dim:"أقصى أبعاد للصور (بكسل)",
    opt_quality:"جودة الصور (%)", opt_start_page:"من صفحة", opt_end_page:"إلى صفحة (0 = النهاية)",
    opt_dpi:"دقة الصور (DPI)", opt_format:"صيغة الصورة", opt_ocr_langs:"لغات التعرّف الضوئي",
    opt_force_all:"تطبيق على كل الفقرات وليس العربية فقط", opt_password:"كلمة المرور الجديدة",
    opt_password_known:"كلمة مرور المستند (إن وجدت)", opt_wm_text:"نص العلامة المائية",
    opt_wm_color:"لون العلامة", opt_wm_opacity:"الشفافية", opt_search:"النص المراد البحث عنه",
    opt_replace:"النص البديل", opt_match_case:"مطابقة حالة الأحرف", opt_whole_word:"كلمات كاملة فقط",
    opt_font:"اسم الخط", opt_font_size:"حجم الخط", opt_keep_emphasis:"الإبقاء على الغامق والمائل",
    stat_words:"الكلمات", stat_characters:"الأحرف", stat_characters_no_spaces:"الأحرف بدون مسافات",
    stat_paragraphs:"الفقرات", stat_tables:"الجداول", stat_images:"الصور", stat_sections:"الأقسام",
    stat_pages:"الصفحات", stat_rtl_ratio:"نسبة النص العربي", stat_reading_time_minutes:"زمن القراءة (دقيقة)",
    tools:{
      "merge-docx":["دمج وورد","جمع عدة مستندات في ملف واحد."],
      "split-docx":["تقسيم وورد","فصل المستند إلى ملفات مستقلة."],
      "compress-docx":["ضغط وورد","تصغير حجم المستند وصوره."],
      "word-to-pdf":["وورد إلى PDF","تحويل المستند إلى PDF ثابت."],
      "pdf-to-docx":["PDF إلى وورد","تحويل PDF إلى ملف قابل للتعديل."],
      "word-to-jpg":["وورد إلى صور","استخراج الصفحات كصور داخل ملف مضغوط."],
      "image-to-docx":["صورة إلى وورد (OCR)","استخراج النصوص من الصور."],
      "rtl-fixer":["إصلاح اتجاه النصوص","ضبط الاتجاه والمحاذاة للنص العربي."],
      "extract-text":["استخراج النصوص","سحب المحتوى النصي كملف TXT."],
      "protect-word":["حماية وورد","تشفير المستند بكلمة مرور."],
      "unlock-word":["إلغاء الحماية","فك التشفير بكلمة المرور وإزالة قيود التحرير."],
      "watermark-word":["علامة مائية","إضافة ختم خلف محتوى المستند."],
      "remove-blank-pages":["حذف الصفحات الفارغة","إزالة الفقرات والفواصل الزائدة."],
      "word-to-html":["وورد إلى HTML","تحويل المستند إلى صفحة ويب."],
      "html-to-word":["HTML إلى وورد","تحويل صفحات الويب إلى مستند."],
      "extract-images":["استخراج الصور","حفظ كل صور المستند في ملف مضغوط."],
      "find-replace":["بحث واستبدال","تغيير الكلمات في كامل المستند."],
      "word-stats":["إحصائيات المستند","عدد الكلمات والصفحات والصور."],
      "markdown-to-word":["ماركداون إلى وورد","تحويل ملفات Markdown إلى مستند."],
      "clean-formatting":["تنظيف التنسيقات","توحيد الخط وإزالة الألوان الزائدة."],
      "remove-metadata":["إزالة بيانات التعريف","حذف اسم المؤلف وسجل التعديلات."],
    }},
  en: { dir:"ltr",
    ui_title:"Everything you need to manage Word files, in one place",
    ui_subtitle:"A complete platform of 21 tools to merge, convert and edit documents.",
    ui_search:"Search tools…", ui_no_results:"No matching tool.",
    ui_footer:"All rights reserved © 2026 — ILoveWord",
    ui_drop:"Click to choose a file, or drop it here", ui_process:"Process",
    ui_processing:"Processing your file…", ui_processing_hint:"Large documents may take a few seconds.",
    ui_done:"Done", ui_download:"Download file", ui_again:"Process another file",
    ui_retry:"Try again", ui_language:"Language", ui_remove:"Remove",
    ui_network:"Could not reach the server. Check that the backend is running.",
    ui_need_file:"Choose a file first.", ui_need_two:"Choose at least two files.",
    ui_required:"This field is required.", ui_accepts:"Accepts:",
    opt_page_break:"Insert a page break between documents", opt_split_mode:"Split by",
    opt_split_pages:"Page breaks", opt_split_count:"Every N paragraphs",
    opt_every:"Paragraphs per part", opt_max_dim:"Max image size (px)",
    opt_quality:"Image quality (%)", opt_start_page:"From page", opt_end_page:"To page (0 = end)",
    opt_dpi:"Resolution (DPI)", opt_format:"Image format", opt_ocr_langs:"OCR languages",
    opt_force_all:"Apply to every paragraph, not just Arabic", opt_password:"New password",
    opt_password_known:"Document password (if any)", opt_wm_text:"Watermark text",
    opt_wm_color:"Colour", opt_wm_opacity:"Opacity", opt_search:"Find",
    opt_replace:"Replace with", opt_match_case:"Match case", opt_whole_word:"Whole words only",
    opt_font:"Font", opt_font_size:"Font size", opt_keep_emphasis:"Keep bold and italic",
    stat_words:"Words", stat_characters:"Characters", stat_characters_no_spaces:"Characters (no spaces)",
    stat_paragraphs:"Paragraphs", stat_tables:"Tables", stat_images:"Images", stat_sections:"Sections",
    stat_pages:"Pages", stat_rtl_ratio:"RTL ratio", stat_reading_time_minutes:"Reading time (min)",
    tools:{
      "merge-docx":["Merge Word","Combine several documents into one."],
      "split-docx":["Split Word","Separate a document into standalone files."],
      "compress-docx":["Compress Word","Shrink the document and its images."],
      "word-to-pdf":["Word to PDF","Convert a document to fixed PDF."],
      "pdf-to-docx":["PDF to Word","Turn a PDF into an editable file."],
      "word-to-jpg":["Word to images","Export every page as an image in a zip."],
      "image-to-docx":["Image to Word (OCR)","Extract text from a picture or scan."],
      "rtl-fixer":["Fix text direction","Correct direction and alignment for Arabic."],
      "extract-text":["Extract text","Pull the raw content out as TXT."],
      "protect-word":["Protect Word","Encrypt the document with a password."],
      "unlock-word":["Remove protection","Decrypt with the password and clear edit restrictions."],
      "watermark-word":["Watermark","Stamp text behind the document content."],
      "remove-blank-pages":["Remove blank pages","Drop empty paragraphs and stray breaks."],
      "word-to-html":["Word to HTML","Convert the document into a web page."],
      "html-to-word":["HTML to Word","Turn a web page into a document."],
      "extract-images":["Extract images","Save every embedded image to a zip."],
      "find-replace":["Find and replace","Change wording across the whole document."],
      "word-stats":["Document statistics","Words, pages, tables and images."],
      "markdown-to-word":["Markdown to Word","Convert Markdown into a document."],
      "clean-formatting":["Clean formatting","Unify the font and strip stray colours."],
      "remove-metadata":["Remove metadata","Delete author, company and revision history."],
    }},
  es: { dir:"ltr",
    ui_title:"Todo lo que necesitas para gestionar archivos Word",
    ui_subtitle:"Una plataforma completa con 21 herramientas para unir, convertir y editar documentos.",
    ui_search:"Buscar herramienta…", ui_no_results:"Ninguna herramienta coincide.",
    ui_footer:"Todos los derechos reservados © 2026 — ILoveWord",
    ui_drop:"Haz clic para elegir un archivo o suéltalo aquí", ui_process:"Procesar",
    ui_processing:"Procesando el archivo…", ui_processing_hint:"Los documentos grandes tardan unos segundos.",
    ui_done:"Listo", ui_download:"Descargar archivo", ui_again:"Procesar otro archivo",
    ui_retry:"Reintentar", ui_language:"Idioma", ui_remove:"Quitar",
    ui_network:"No se pudo conectar con el servidor. Comprueba que el backend esté activo.",
    ui_need_file:"Elige un archivo primero.", ui_need_two:"Elige al menos dos archivos.",
    ui_required:"Este campo es obligatorio.", ui_accepts:"Formatos:",
    opt_page_break:"Insertar salto de página entre documentos", opt_split_mode:"Dividir por",
    opt_split_pages:"Saltos de página", opt_split_count:"Cada N párrafos",
    opt_every:"Párrafos por parte", opt_max_dim:"Tamaño máx. de imagen (px)",
    opt_quality:"Calidad de imagen (%)", opt_start_page:"Desde la página", opt_end_page:"Hasta la página (0 = final)",
    opt_dpi:"Resolución (DPI)", opt_format:"Formato de imagen", opt_ocr_langs:"Idiomas de OCR",
    opt_force_all:"Aplicar a todos los párrafos", opt_password:"Nueva contraseña",
    opt_password_known:"Contraseña del documento (si tiene)", opt_wm_text:"Texto de la marca de agua",
    opt_wm_color:"Color", opt_wm_opacity:"Opacidad", opt_search:"Buscar",
    opt_replace:"Reemplazar por", opt_match_case:"Distinguir mayúsculas", opt_whole_word:"Solo palabras completas",
    opt_font:"Fuente", opt_font_size:"Tamaño de fuente", opt_keep_emphasis:"Mantener negrita y cursiva",
    stat_words:"Palabras", stat_characters:"Caracteres", stat_characters_no_spaces:"Caracteres (sin espacios)",
    stat_paragraphs:"Párrafos", stat_tables:"Tablas", stat_images:"Imágenes", stat_sections:"Secciones",
    stat_pages:"Páginas", stat_rtl_ratio:"Proporción RTL", stat_reading_time_minutes:"Lectura (min)",
    tools:{
      "merge-docx":["Unir Word","Combina varios documentos en uno."],
      "split-docx":["Dividir Word","Separa el documento en archivos independientes."],
      "compress-docx":["Comprimir Word","Reduce el tamaño del documento y sus imágenes."],
      "word-to-pdf":["Word a PDF","Convierte el documento a PDF."],
      "pdf-to-docx":["PDF a Word","Convierte un PDF en un archivo editable."],
      "word-to-jpg":["Word a imágenes","Exporta cada página como imagen en un zip."],
      "image-to-docx":["Imagen a Word (OCR)","Extrae el texto de una imagen o escaneo."],
      "rtl-fixer":["Corregir dirección","Ajusta dirección y alineación del texto árabe."],
      "extract-text":["Extraer texto","Obtén el contenido como TXT."],
      "protect-word":["Proteger Word","Cifra el documento con contraseña."],
      "unlock-word":["Quitar protección","Descifra con la contraseña y quita restricciones."],
      "watermark-word":["Marca de agua","Añade un sello detrás del contenido."],
      "remove-blank-pages":["Quitar páginas vacías","Elimina párrafos vacíos y saltos sobrantes."],
      "word-to-html":["Word a HTML","Convierte el documento en página web."],
      "html-to-word":["HTML a Word","Convierte una página web en documento."],
      "extract-images":["Extraer imágenes","Guarda todas las imágenes en un zip."],
      "find-replace":["Buscar y reemplazar","Cambia palabras en todo el documento."],
      "word-stats":["Estadísticas","Palabras, páginas, tablas e imágenes."],
      "markdown-to-word":["Markdown a Word","Convierte Markdown en documento."],
      "clean-formatting":["Limpiar formato","Unifica la fuente y quita colores sobrantes."],
      "remove-metadata":["Quitar metadatos","Borra autor, empresa e historial."],
    }},
  fr: { dir:"ltr",
    ui_title:"Tout ce qu'il faut pour gérer vos fichiers Word",
    ui_subtitle:"Une plateforme complète de 21 outils pour fusionner, convertir et modifier vos documents.",
    ui_search:"Rechercher un outil…", ui_no_results:"Aucun outil correspondant.",
    ui_footer:"Tous droits réservés © 2026 — ILoveWord",
    ui_drop:"Cliquez pour choisir un fichier ou déposez-le ici", ui_process:"Traiter",
    ui_processing:"Traitement du fichier…", ui_processing_hint:"Les documents volumineux prennent quelques secondes.",
    ui_done:"Terminé", ui_download:"Télécharger le fichier", ui_again:"Traiter un autre fichier",
    ui_retry:"Réessayer", ui_language:"Langue", ui_remove:"Retirer",
    ui_network:"Impossible de joindre le serveur. Vérifiez que le backend fonctionne.",
    ui_need_file:"Choisissez d'abord un fichier.", ui_need_two:"Choisissez au moins deux fichiers.",
    ui_required:"Ce champ est obligatoire.", ui_accepts:"Formats :",
    opt_page_break:"Insérer un saut de page entre les documents", opt_split_mode:"Découper par",
    opt_split_pages:"Sauts de page", opt_split_count:"Tous les N paragraphes",
    opt_every:"Paragraphes par partie", opt_max_dim:"Taille max. des images (px)",
    opt_quality:"Qualité des images (%)", opt_start_page:"De la page", opt_end_page:"À la page (0 = fin)",
    opt_dpi:"Résolution (DPI)", opt_format:"Format d'image", opt_ocr_langs:"Langues OCR",
    opt_force_all:"Appliquer à tous les paragraphes", opt_password:"Nouveau mot de passe",
    opt_password_known:"Mot de passe du document (le cas échéant)", opt_wm_text:"Texte du filigrane",
    opt_wm_color:"Couleur", opt_wm_opacity:"Opacité", opt_search:"Rechercher",
    opt_replace:"Remplacer par", opt_match_case:"Respecter la casse", opt_whole_word:"Mots entiers uniquement",
    opt_font:"Police", opt_font_size:"Taille de police", opt_keep_emphasis:"Conserver gras et italique",
    stat_words:"Mots", stat_characters:"Caractères", stat_characters_no_spaces:"Caractères (sans espaces)",
    stat_paragraphs:"Paragraphes", stat_tables:"Tableaux", stat_images:"Images", stat_sections:"Sections",
    stat_pages:"Pages", stat_rtl_ratio:"Proportion RTL", stat_reading_time_minutes:"Lecture (min)",
    tools:{
      "merge-docx":["Fusionner Word","Combinez plusieurs documents en un seul."],
      "split-docx":["Diviser Word","Séparez le document en fichiers distincts."],
      "compress-docx":["Compresser Word","Réduisez la taille du document et de ses images."],
      "word-to-pdf":["Word en PDF","Convertissez le document en PDF."],
      "pdf-to-docx":["PDF en Word","Transformez un PDF en fichier modifiable."],
      "word-to-jpg":["Word en images","Exportez chaque page en image dans un zip."],
      "image-to-docx":["Image en Word (OCR)","Extrayez le texte d'une image ou d'un scan."],
      "rtl-fixer":["Corriger la direction","Ajustez direction et alignement du texte arabe."],
      "extract-text":["Extraire le texte","Récupérez le contenu au format TXT."],
      "protect-word":["Protéger Word","Chiffrez le document par mot de passe."],
      "unlock-word":["Retirer la protection","Déchiffrez avec le mot de passe et levez les restrictions."],
      "watermark-word":["Filigrane","Ajoutez un tampon derrière le contenu."],
      "remove-blank-pages":["Supprimer les pages vides","Retirez paragraphes vides et sauts inutiles."],
      "word-to-html":["Word en HTML","Convertissez le document en page web."],
      "html-to-word":["HTML en Word","Transformez une page web en document."],
      "extract-images":["Extraire les images","Enregistrez toutes les images dans un zip."],
      "find-replace":["Rechercher et remplacer","Modifiez les mots dans tout le document."],
      "word-stats":["Statistiques","Mots, pages, tableaux et images."],
      "markdown-to-word":["Markdown en Word","Convertissez du Markdown en document."],
      "clean-formatting":["Nettoyer la mise en forme","Unifiez la police et retirez les couleurs."],
      "remove-metadata":["Supprimer les métadonnées","Effacez auteur, société et historique."],
    }},
};

let lang = (localStorage.getItem("ilw_lang")
  || (navigator.language || "ar").slice(0, 2));
if (!I18N[lang]) lang = "ar";
const t = (key) => I18N[lang][key] ?? I18N.en[key] ?? key;
const toolText = (id) => I18N[lang].tools[id] || I18N.en.tools[id] || [id, ""];

/* ------------------------------------------------------------------ *
 * Grid
 * ------------------------------------------------------------------ */
const $ = (id) => document.getElementById(id);
const icon = (name, cls = "") =>
  `<svg class="icon ${cls}" aria-hidden="true"><use href="assets/icons.svg#${name}"></use></svg>`;
const grid = $("grid");

function renderGrid(filter = "") {
  const needle = filter.trim().toLowerCase();
  grid.textContent = "";
  let shown = 0;
  for (const tool of TOOLS) {
    const [title, desc] = toolText(tool.id);
    if (needle && !(`${title} ${desc} ${tool.id}`.toLowerCase().includes(needle))) continue;
    shown++;
    const card = document.createElement("button");
    card.type = "button";
    card.className = "tool-card bg-white p-6 rounded-2xl border border-gray-100 shadow-sm cursor-pointer flex flex-col items-center text-center";
    card.addEventListener("click", () => openTool(tool));

    const badge = document.createElement("div");
    badge.className = `w-14 h-14 rounded-2xl flex items-center justify-center text-2xl mb-4 ${TONES[tool.tone] || TONES.gray}`;
    badge.innerHTML = icon(tool.icon);

    const h3 = document.createElement("h3");
    h3.className = "font-bold text-gray-800 text-base mb-1";
    h3.textContent = title;

    const p = document.createElement("p");
    p.className = "text-gray-500 text-xs leading-relaxed";
    p.textContent = desc;

    card.append(badge, h3, p);
    grid.append(card);
  }
  $("no-results").hidden = shown !== 0;
}

function applyLanguage(next) {
  lang = I18N[next] ? next : "ar";
  localStorage.setItem("ilw_lang", lang);
  document.documentElement.lang = lang;
  document.documentElement.dir = I18N[lang].dir;
  $("langSelect").value = lang;
  document.querySelectorAll("[data-i18n]").forEach(el => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-placeholder]").forEach(el => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
  renderGrid($("search").value);
  if (active) openTool(active, true);
}

/* ------------------------------------------------------------------ *
 * Modal
 * ------------------------------------------------------------------ */
let active = null;
let files = [];
let lastUrl = null;

const STATES = ["state-idle", "state-busy", "state-done", "state-error", "state-report"];
function show(state) { STATES.forEach(s => { $(s).hidden = s !== state; }); }

function openTool(tool, keepFiles = false) {
  active = tool;
  if (!keepFiles) files = [];
  const [title, desc] = toolText(tool.id);
  $("modal-title").textContent = title;
  $("modal-desc").textContent = desc;
  $("modal-icon").className = `w-14 h-14 rounded-2xl mx-auto flex items-center justify-center text-2xl mb-3 ${TONES[tool.tone] || TONES.gray}`;
  $("modal-icon").innerHTML = icon(tool.icon);
  $("accept-hint").textContent = `${t("ui_accepts")} ${tool.accept.replace(/,/g, "  ")}`;
  const input = $("file-input");
  input.accept = tool.accept;
  input.multiple = Boolean(tool.multiple);
  input.value = "";
  renderOptions(tool);
  renderFileList();
  show("state-idle");
  $("modal").hidden = false;
  $("modal").classList.add("flex");
  $("dropzone").focus();
}

function closeModal() {
  $("modal").hidden = true;
  $("modal").classList.remove("flex");
  active = null; files = [];
  if (lastUrl) { URL.revokeObjectURL(lastUrl); lastUrl = null; }
}

function renderOptions(tool) {
  const box = $("options");
  box.textContent = "";
  for (const opt of tool.options || []) {
    const wrap = document.createElement("div");
    wrap.dataset.optionName = opt.name;

    const label = document.createElement("label");
    label.className = "block text-sm font-semibold text-gray-700 mb-1";
    label.htmlFor = `opt-${opt.name}`;
    label.textContent = t(opt.label) || opt.label;

    let field;
    if (opt.type === "select") {
      field = document.createElement("select");
      for (const [value, key] of opt.choices) {
        const o = document.createElement("option");
        o.value = value;
        o.textContent = I18N[lang][key] || key;
        field.append(o);
      }
    } else if (opt.type === "checkbox") {
      field = document.createElement("input");
      field.type = "checkbox";
      field.checked = Boolean(opt.value);
    } else {
      field = document.createElement("input");
      field.type = opt.type;
      if (opt.placeholder) field.placeholder = opt.placeholder;
      if (opt.min !== undefined) field.min = opt.min;
      if (opt.max !== undefined) field.max = opt.max;
      if (opt.step !== undefined) field.step = opt.step;
      if (opt.maxlength) field.maxLength = opt.maxlength;
    }
    field.id = `opt-${opt.name}`;
    field.name = opt.name;
    if (opt.type !== "checkbox") field.value = opt.value;

    if (opt.type === "checkbox") {
      wrap.className = "flex items-center gap-2";
      field.className = "w-4 h-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500";
      label.className = "text-sm text-gray-700 select-none mb-0";
      wrap.append(field, label);
    } else {
      field.className = "w-full border border-gray-300 rounded-xl px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500";
      if (opt.type === "color") field.className = "h-10 w-20 border border-gray-300 rounded-xl p-1";
      wrap.append(label, field);
    }
    if (opt.showWhen) wrap.dataset.showWhen = JSON.stringify(opt.showWhen);
    box.append(wrap);
  }
  box.addEventListener("change", syncConditionals);
  syncConditionals();
}

function syncConditionals() {
  $("options").querySelectorAll("[data-show-when]").forEach(wrap => {
    const rule = JSON.parse(wrap.dataset.showWhen);
    const ok = Object.entries(rule).every(([name, value]) => {
      const el = $(`opt-${name}`);
      return el && el.value === value;
    });
    wrap.hidden = !ok;
  });
}

function renderFileList() {
  const list = $("file-list");
  list.textContent = "";
  files.forEach((file, index) => {
    const li = document.createElement("li");
    li.className = "flex items-center justify-between gap-2 bg-gray-50 border border-gray-200 rounded-xl px-3 py-2";
    const name = document.createElement("span");
    name.className = "truncate text-gray-700";
    name.textContent = `${file.name} · ${(file.size / 1024).toFixed(0)} KB`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "text-gray-400 hover:text-red-600 shrink-0";
    remove.setAttribute("aria-label", t("ui_remove"));
    remove.innerHTML = icon("fa-xmark");
    remove.addEventListener("click", () => { files.splice(index, 1); renderFileList(); });
    li.append(name, remove);
    list.append(li);
  });
  const enough = active?.multiple ? files.length >= 2 : files.length === 1;
  $("run").disabled = !enough;
}

function acceptFiles(incoming) {
  const chosen = Array.from(incoming);
  if (!chosen.length) return;
  files = active?.multiple ? files.concat(chosen) : [chosen[0]];
  renderFileList();
}

/* ------------------------------------------------------------------ *
 * Request
 * ------------------------------------------------------------------ */
function filenameFrom(response, fallback) {
  const header = response.headers.get("Content-Disposition") || "";
  const star = header.match(/filename\*=UTF-8''([^;]+)/i);
  if (star) { try { return decodeURIComponent(star[1]); } catch { /* fall through */ } }
  const plain = header.match(/filename="?([^";]+)"?/i);
  return plain ? plain[1] : fallback;
}

async function submit() {
  if (!active) return;
  if (!files.length) return fail(t("ui_need_file"));
  if (active.multiple && files.length < 2) return fail(t("ui_need_two"));

  const body = new FormData();
  for (const file of files) body.append(active.field || "file", file);

  for (const opt of active.options || []) {
    const el = $(`opt-${opt.name}`);
    if (!el) continue;
    const wrap = el.closest("[data-option-name]");
    if (wrap?.hidden) continue;
    let value = opt.type === "checkbox" ? el.checked : el.value;
    if (opt.required && String(value).trim() === "") {
      return fail(`${t(opt.label)}: ${t("ui_required")}`);
    }
    if (opt.type === "color") value = String(value).replace("#", "").toUpperCase();
    // FastAPI's bool parser accepts "true"/"false"; never send "on".
    body.append(opt.name, opt.type === "checkbox" ? String(value) : value);
  }

  show("state-busy");
  let response;
  try {
    response = await fetch(API + active.path, { method: "POST", body });
  } catch {
    return fail(t("ui_network"));
  }

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      if (data && data.detail) message = data.detail;
    } catch { /* non-JSON error body */ }
    return fail(message);
  }

  const type = response.headers.get("Content-Type") || "";
  if (active.json || type.includes("application/json")) {
    const data = await response.json();
    return report(data.stats || data);
  }

  const blob = await response.blob();
  if (lastUrl) URL.revokeObjectURL(lastUrl);
  lastUrl = URL.createObjectURL(blob);
  const name = filenameFrom(response, `${files[0].name.replace(/\.[^.]+$/, "")}_processed`);
  const link = $("download");
  link.href = lastUrl;
  link.download = name;
  $("done-name").textContent = `${name} · ${(blob.size / 1024).toFixed(0)} KB`;
  show("state-done");
}

function report(stats) {
  const body = $("report-body");
  body.textContent = "";
  for (const [key, value] of Object.entries(stats)) {
    const dt = document.createElement("dt");
    dt.className = "text-gray-500";
    dt.textContent = t(`stat_${key}`);
    const dd = document.createElement("dd");
    dd.className = "font-bold text-gray-900 text-end";
    dd.textContent = value;
    body.append(dt, dd);
  }
  show("state-report");
}

function fail(message) {
  $("error-text").textContent = message;
  show("state-error");
}

/* ------------------------------------------------------------------ *
 * Wiring
 * ------------------------------------------------------------------ */
$("langSelect").addEventListener("change", (e) => applyLanguage(e.target.value));
$("search").addEventListener("input", (e) => renderGrid(e.target.value));
$("modal-close").addEventListener("click", closeModal);
$("modal").addEventListener("click", (e) => { if (e.target === $("modal")) closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("modal").hidden) closeModal(); });

$("dropzone").addEventListener("click", () => $("file-input").click());
$("dropzone").addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("file-input").click(); }
});
$("file-input").addEventListener("change", (e) => acceptFiles(e.target.files));
["dragenter", "dragover"].forEach(evt => $("dropzone").addEventListener(evt, (e) => {
  e.preventDefault(); $("dropzone").classList.add("border-blue-600", "bg-blue-100/60");
}));
["dragleave", "drop"].forEach(evt => $("dropzone").addEventListener(evt, (e) => {
  e.preventDefault(); $("dropzone").classList.remove("border-blue-600", "bg-blue-100/60");
}));
$("dropzone").addEventListener("drop", (e) => acceptFiles(e.dataTransfer.files));

$("run").addEventListener("click", submit);
$("retry").addEventListener("click", () => show("state-idle"));
$("again").addEventListener("click", () => openTool(active));
$("again2").addEventListener("click", () => openTool(active));

applyLanguage(lang);

// Surface degraded capability up front instead of failing mid-upload.
fetch(API + "/health").then(r => r.json()).then(data => {
  const off = Object.entries(data.engines).filter(([, up]) => !up).map(([n]) => n);
  const badge = document.getElementById("engine-badge");
  badge.textContent = off.length ? `⚠ ${off.join(", ")}` : `v${data.version}`;
  badge.title = off.length ? "Some conversion engines are unavailable on the server." : "";
  if (off.length) badge.className = "hidden sm:inline text-xs px-2 py-1 rounded-full bg-amber-100 text-amber-700";
}).catch(() => {});
