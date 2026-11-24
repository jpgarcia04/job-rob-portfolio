/* =========================================================================
   Bandeja de ofertas — lógica de la interfaz

   JavaScript vanilla, sin framework ni empaquetador. Todo el estado vive en
   el objeto `estado`; cuando cambia algo, se vuelve a pintar. Sin
   sincronizaciones sutiles que luego nadie sabe depurar.

   Sin atajos de teclado: se navega con el ratón. La única tecla que responde
   es Escape para cerrar el modal, que no es un atajo sino lo que cualquiera
   espera de una ventana — y lo que necesita quien navega con teclado.

   DOS REGLAS QUE ESTE ARCHIVO TIENE QUE CUMPLIR
   ---------------------------------------------
   1. Ningún camino lanza una corrida sin haber pasado por `confirmar()`. La
      app no gasta un dólar sin haber enseñado antes el número. El servidor lo
      vuelve a comprobar, pero eso es la red de seguridad, no la puerta.

   2. Una oferta APLAZADA no se pinta como una descartada. El triaje la vio,
      la ordenó y escribió por qué; si aquí se viera igual que el ruido,
      habríamos reconstruido el filtro silencioso con pasos de más.
   ========================================================================= */

'use strict';

const estado = {
  filtro: 'nueva',
  mercado: 'todos',
  zona: 'todas',        // solo en mercados con zonas (Texas); ver `pintarZonas`
  relevancia: 'todas',  // lo que decidió la capa 2
  ofertas: [],
  abierta: null,        // oferta mostrada en el modal
  focoPrevio: null,     // a dónde devolver el foco al cerrar
  hayTriaje: false,     // ¿ha corrido el triaje alguna vez?
  trabajo: null,        // el trabajo que se está siguiendo
  reloj: null,          // el temporizador que lo sigue
  confirmando: null,    // callback del diálogo de confirmación
  presupuesto: null,    // lo último que dijo /api/presupuesto (ver `abrirConfirmar`)
};

const $ = (sel) => document.querySelector(sel);

/* Dónde está MEDIDO que la fuente bloquea el anuncio por región. No es una
   lista de mercados: es una lista de webs. Ver `pintarFicha`. */
const DOMINIOS_QUE_BLOQUEAN = ['www.adzuna.com', 'adzuna.com'];

/* DOS TEXTOS POR BANDEJA, Y LA DIFERENCIA ES LA QUE ELLA NECESITA (7 sept 2026).

   Con un filtro puesto, el vacío NO significa que no haya guardado nada:
   significa que lo guardado no pasa ese filtro. El texto de antes decía lo
   segundo siempre, y en «Interesan» eso le hace creer que ha perdido sus
   ofertas — que es la regla nº 3 rota justo donde más asusta. `pintarRejilla`
   elige según los filtros que de verdad hay puestos. */
const TEXTOS_VACIO = {
  nueva: {
    limpio: 'No hay ofertas sin revisar. Pulsa «Buscar ofertas nuevas» para traer más.',
    filtrado: 'No hay ofertas sin revisar con estos filtros. Prueba «Todos los ' +
              'mercados» y «Todas»; si sigue vacío, pulsa «Buscar ofertas nuevas».',
  },
  interesa: {
    limpio: 'Todavía no has marcado ninguna oferta como interesante.',
    filtrado: 'No hay ofertas interesantes con estos filtros. Elige «Todos los ' +
              'mercados» y «Todas» en «Lectura del sistema» para ver las que has guardado.',
  },
  postulada: {
    limpio: 'Todavía no has marcado ninguna como enviada.',
    filtrado: 'No hay ofertas enviadas con estos filtros. Elige «Todos los ' +
              'mercados» y «Todas» en «Lectura del sistema» para ver las que has marcado.',
  },
  descartada: {
    limpio: 'No has descartado ninguna oferta.',
    filtrado: 'No hay ofertas descartadas con estos filtros. Elige «Todos los ' +
              'mercados» y «Todas» en «Lectura del sistema» para ver las que has descartado.',
  },
};

/* Lo que hay que explicar de cada filtro, y solo se explica lo que engaña si
   no se explica. «Sin revisar todavía» es el caso: sin este texto se lee como
   «descartadas», y entonces el triaje se convierte en el filtro silencioso
   que este proyecto no se permite. */
const EXPLICACION = {
  aplazadas:
    'El asistente vio estas ofertas por encima —título, empresa, ubicación y ' +
    'sueldo—, las ordenó y las dejó para después. NO están descartadas: nadie ' +
    'ha leído todavía su descripción, así que no hay ningún veredicto sobre ' +
    'ellas. Cada oferta tiene un solo turno de triaje, o sea que estas no ' +
    'vuelven a competir solas: ábrela y pulsa «que la lea el asistente».',
  sin_mirar:
    'Nadie ha mirado estas ofertas todavía, ni siquiera por encima. Entran ' +
    'solas en la próxima corrida de «que el asistente lea lo nuevo».',
  descartadas:
    'El asistente leyó estas enteras y explicó por qué no encajan. Se enseñan ' +
    'igualmente: si su criterio se equivoca, tienes que poder verlo.',
};

/* ---------- utilidades ---------- */

function fechaCorta(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return '—';

  const dias = Math.floor((Date.now() - d.getTime()) / 86400000);
  if (dias <= 0) return 'hoy';
  if (dias === 1) return 'ayer';
  if (dias < 30) return `hace ${dias} d`;
  return d.toLocaleDateString('es-MX', { day: 'numeric', month: 'short' });
}

/* EL SUELDO QUE NO DICE SU UNIDAD, Y POR QUÉ NO SE ADIVINA.

   De los 370 anuncios con sueldo de la base, 22 traen cifras de tres dígitos
   o menos. Medido sobre lo que devuelve Adzuna el 9 septiembre 2026:

       General Counsel/CAO (Dallas) ........ 550 – 1200   -> miles al año
       Vice President, Supply Chain ........ 180 – 280    -> miles al año
       German Speaking Sales Rep ...........  18 – 22     -> POR HORA
       Sales Director Europe ...............   1 – (nada) -> basura

   La misma API, el mismo campo, tres unidades distintas y ninguna declarada:
   `contract_time` viene vacío en casi todas. Y no es una duda académica —a
   ella le interesan los encargos interinos y la consultoría (ver cvs.yaml),
   donde «180 – 280 por hora» es una tarifa perfectamente normal y «180 – 280
   mil al año» también.

   Así que no se multiplica por mil ni se esconde: se dice que la fuente no
   dijo la unidad. La tarjeta enseñaba «180 – 260 USD» al lado de otra que
   decía «173,118 USD», y de las dos solo una significaba lo que parecía. */
const SUELDO_ANUAL_MINIMO_CREIBLE = 1000;

function sueldoSinUnidad(o) {
  const max = o.salary_max || o.salary_min;
  return max != null && max > 0 && max < SUELDO_ANUAL_MINIMO_CREIBLE;
}

function salarioTexto(o) {
  if (o.salary_min || o.salary_max) {
    const min = o.salary_min || o.salary_max;
    const max = o.salary_max || o.salary_min;
    const fmt = (n) => Math.round(n).toLocaleString('es-MX');
    const rango = min === max ? fmt(min) : `${fmt(min)} – ${fmt(max)}`;
    const cabeza = `${rango} ${o.salary_currency || ''}`.trim();
    return sueldoSinUnidad(o) ? `${cabeza} sin unidad` : cabeza;
  }
  return o.salary_raw || null;
}

function avisar(texto) {
  const aviso = $('#aviso');
  aviso.textContent = texto;
  aviso.hidden = false;
  clearTimeout(avisar._t);
  avisar._t = setTimeout(() => { aviso.hidden = true; }, 3800);
}

/** Crea un elemento con clase y texto. Evita repetir tres líneas cada vez.
 *  Se usa `textContent`, nunca `innerHTML`, para todo lo que venga de la API:
 *  los títulos y descripciones son texto ajeno y podrían traer HTML dentro. */
function el(tag, clase, texto) {
  const n = document.createElement(tag);
  if (clase) n.className = clase;
  if (texto != null) n.textContent = texto;
  return n;
}

const dolares = (n) => `$${Number(n || 0).toFixed(4)}`;

/* ---------- lectura de la clasificación ----------
   Cinco estados posibles y ninguno más. Los dos últimos son la mitad del
   diseño del triaje: `aplazada` es «la vio y la dejó para después, con un
   motivo escrito», `pendiente` es «nadie la ha mirado nunca». Que se puedan
   distinguir es la regla nº 1 de la casa. */

function veredictoDe(o) {
  if (o.clas_estado == null) {
    return o.triaje_posicion != null ? 'aplazada' : 'pendiente';
  }
  if (o.clas_estado !== 'ok')   return 'fallida';
  if (o.clas_necesita_criterio) return 'criterio';
  return o.clas_relevante ? 'relevante' : 'ruido';
}

const ETIQUETA_VEREDICTO = {
  relevante: 'Relevante',
  criterio:  'Tu criterio',
  ruido:     'No encaja',
  fallida:   'Sin clasificar',
  aplazada:  'Sin revisar todavía',
  pendiente: 'Nadie la ha mirado',
};

const NIVEL = {
  c_level:     'C-level',
  direccion:   'Dirección',
  gerencia:    'Gerencia',
  individual:  'Sin equipo a cargo',
  desconocido: 'Nivel no indicado',
};

const MODALIDAD = {
  presencial:  'Presencial',
  hibrido:     'Híbrido',
  remoto:      'Remoto',
  desconocido: 'Modalidad no indicada',
};

const ICONO_LUGAR =
  '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">' +
  '<path d="M8 1.6c-2.4 0-4.3 1.9-4.3 4.3C3.7 9.2 8 14.4 8 14.4s4.3-5.2 4.3-8.5' +
  'c0-2.4-1.9-4.3-4.3-4.3z" fill="none" stroke="currentColor" stroke-width="1.3"/>' +
  '<circle cx="8" cy="5.9" r="1.5" fill="currentColor"/></svg>';

/* ---------- datos ---------- */

async function pedir(url, opciones) {
  const res = await fetch(url, opciones);
  if (!res.ok) {
    let detalle = `HTTP ${res.status}`;
    try {
      const cuerpo = await res.json();
      if (cuerpo.detail) detalle = cuerpo.detail;
    } catch (_) { /* la respuesta no era JSON */ }
    const error = new Error(detalle);
    // El código importa: 402 es "no cabe en el presupuesto" y 409 es "hay
    // otra corrida" o "no hay nada que hacer". Son cosas distintas y ella
    // tiene que poder distinguirlas.
    error.status = res.status;
    throw error;
  }
  return res.json();
}

/* LOS CONTADORES SON DE LA PANTALLA QUE ELLA TIENE DELANTE.

   Se le mandan los filtros puestos porque, sin ellos, la pastilla contaba toda
   la base y la rejilla enseñaba un trozo. Con México elegido, el 4 septiembre
   2026: la pastilla decía «Relevantes 59» sobre una rejilla de 3 tarjetas. Y
   ahora cuentan VACANTES, que es lo que se ve al pulsar — regla nº 14. */
/* LA RESPUESTA QUE LLEGA TARDE NO PINTA (24 septiembre 2026).

   Visto ejecutando al estrenar las zonas: pulsar un estado y cambiar de
   mercado sin esperar dejó la pantalla de Alemania con las 8 tarjetas del
   Valle y la fila de zonas de Texas encima. Las dos peticiones iban en vuelo
   y ganó la que llegó la última, que era la vieja. Cada carga apunta su turno
   y solo pinta si sigue siendo la más reciente cuando vuelve. */
const turnos = { resumen: 0, ofertas: 0 };

async function cargarResumen() {
  const turno = ++turnos.resumen;
  const data = await pedir(
    `/api/resumen?estado=${encodeURIComponent(estado.filtro)}`
    + `&mercado=${encodeURIComponent(estado.mercado)}`
    + `&zona=${encodeURIComponent(estado.zona)}`);
  if (turno !== turnos.resumen) return;

  for (const [clave, n] of Object.entries(data.por_estado)) {
    const nodo = document.querySelector(`[data-cuenta="${clave}"]`);
    if (nodo) nodo.textContent = n;
  }

  // ANUNCIOS, Y SE DICE (8 septiembre 2026). `total` es `total_offers()`, que
  // cuenta ANUNCIOS; la pastilla de al lado, «Sin revisar 272», cuenta
  // VACANTES. Los dos numeros eran correctos y la palabra «ofertas» los ponia
  // en la misma unidad, asi que uno de los dos parecia estar mal. Se nombra la
  // unidad de cada uno y el desajuste deja de serlo.
  //
  // LOS MERCADOS SE CUENTAN AQUI Y SE NOMBRAN EN EL DESPLEGABLE (22 sept 2026).
  //
  // Aqui iba la lista entera —«477 anuncios · Alemania · Mexico · Texas,
  // EE.UU.»— y NO CABIA. Medido ejecutando a 1360 px, que es el ancho de la
  // ventana real: el subtitulo pide 322 px y tiene 165, asi que se cortaba en
  // «477 anuncios · Alemania…». Con dos mercados ya se comia Texas; con tres,
  // dos de los tres. O sea que la linea que existia para decirle donde busca
  // el sistema estaba diciendo justo lo contrario.
  //
  // Y NO SE ARREGLA DANDOLE SITIO: `.marca` tiene `flex-shrink: 6` a
  // proposito desde el 11 septiembre, para que el subtitulo ceda y el
  // selector de mercado conserve su texto en vez de encimarse con el contador
  // de gasto. Esa decision sigue siendo la buena.
  //
  // Asi que el dato se muda a donde si cabe y ademas queda mejor: el
  // desplegable de al lado ya lista «Alemania (49)», «Mexico (32)», «Texas,
  // EE.UU. (316)» con sus recuentos, y ese no se recorta. Aqui queda cuantos
  // son —que es lo que se ve sin abrir nada, y lo que cambia cuando entra un
  // mercado nuevo— y los nombres siguen enteros al pasar el raton.
  const nombres = data.mercados.map((m) => m.etiqueta);
  const cuantos = nombres.length === 1 ? '1 mercado' : `${nombres.length} mercados`;
  const sub = $('#marca-sub');
  sub.textContent = `${data.total} anuncios · ${cuantos}`;
  sub.title = nombres.join(' · ');

  const select = $('#mercado');
  if (select.options.length === 1) {
    for (const m of data.mercados) {
      const op = document.createElement('option');
      op.value = m.clave;
      op.textContent = `${m.etiqueta} (${m.total})`;
      select.appendChild(op);
    }
  }

  pintarZonas(data.zonas || [], data.zonas_total || 0);

  estado.hayTriaje = (data.triaje && data.triaje.triadas > 0) || false;
  pintarBarraClasificacion(data.clasificacion, data.triaje, data.vistas);
}

/* LAS ZONAS DE UN MERCADO (24 septiembre 2026).

   Texas pasó de entero a cuatro zonas —Houston, Austin, San Antonio, y el
   Valle con el sur—, que son la parte del estado que ella marcó en un mapa.
   El desplegable sigue siendo de MERCADOS: esto es una fila aparte que solo
   sale con un mercado que tenga zonas, y que se vuelve a dibujar en cada
   resumen porque sus cifras cuentan bajo el estado elegido.

   Los nombres de las ciudades de cada zona van en el `title`: «Valle y sur de
   Texas» dicho a secas no dice si Laredo está dentro. */
function pintarZonas(zonas, total) {
  const fila = $('#fila-zona');
  const nav = $('#filtros-zona');
  if (!zonas.length) {
    fila.hidden = true;
    nav.innerHTML = '';
    return;
  }
  // Una zona que ya no existe (se cambió el YAML) no puede quedarse puesta:
  // filtraría a cero sin que ningún botón lo diga.
  if (estado.zona !== 'todas' && !zonas.some((z) => z.clave === estado.zona)) {
    estado.zona = 'todas';
  }
  nav.innerHTML = '';
  // LAS ZONAS PUEDEN SUMAR MÁS QUE «TODAS», y no es un fallo. Medido al
  // encenderlas: Todas 174, y las cuatro zonas 74 + 86 + 22 + 8 = 190. Una
  // vacante publicada en Houston y en Austin es UNA tarjeta en «Todas» y sale
  // en las dos zonas, porque en las dos se puede trabajar. Dos números que no
  // cuadran se leen como un error del programa, así que se dice.
  const suma = zonas.reduce((n, z) => n + z.total, 0);
  const opciones = [
    {
      clave: 'todas', etiqueta: 'Todas', total,
      descripcion: suma > total
        ? 'Una vacante publicada en varias zonas sale en cada una de ellas, '
          + 'por eso las zonas suman más que «Todas».'
        : '',
    },
    ...zonas,
  ];
  for (const z of opciones) {
    const activo = z.clave === estado.zona;
    const boton = el('button', 'filtro' + (activo ? ' activo' : ''), z.etiqueta);
    boton.type = 'button';
    boton.dataset.zona = z.clave;
    boton.setAttribute('aria-pressed', String(activo));
    if (z.descripcion) boton.title = z.descripcion;
    boton.append(el('span', 'pastilla', String(z.total)));
    nav.append(boton);
  }
  fila.hidden = false;
}

/** La fila de filtros de la capa 2 solo aparece si hay algo clasificado o
 *  triado. Sin ninguna de las dos capas corridas, la bandeja es la de antes. */
function pintarBarraClasificacion(c, t, vistas) {
  const fila = $('#fila-relevancia');
  const hayAlgo = (c && c.total) || (t && t.triadas);
  if (!hayAlgo) { fila.hidden = true; return; }
  fila.hidden = false;

  // `vistas` son vacantes del filtro puesto — lo que la rejilla va a enseñar.
  // `c` y `t` siguen contando toda la base, y por eso ya no se pintan aquí:
  // solo deciden si esta fila de filtros existe.
  const cuentas = vistas || {
    relevantes:  c.relevantes,
    criterio:    c.necesita_criterio + c.sin_clasificar,
    descartadas: c.descartadas,
    aplazadas:   (t && t.aplazadas) || 0,
    sin_mirar:   (t && t.sin_mirar) || 0,
  };
  for (const [clave, n] of Object.entries(cuentas)) {
    const nodo = document.querySelector(`[data-rel-cuenta="${clave}"]`);
    if (nodo) nodo.textContent = n;
  }

  // AQUÍ SE PINTABA `86 clasificadas · $1.1016 USD · ejecutivo_en: 70 · …`.
  // Ya no: no cabía —se cortaba— y no le decía nada a ella. Ese desglose vive
  // en `python -m clasificador presupuesto`, entero y con contexto.
}

async function cargarOfertas() {
  const turno = ++turnos.ofertas;
  $('#cargando').hidden = false;
  $('#vacio').hidden = true;
  $('#rejilla').innerHTML = '';

  const explicacion = $('#explicacion');
  const texto = EXPLICACION[estado.relevancia];
  explicacion.textContent = texto || '';
  explicacion.hidden = !texto;

  try {
    const url = `/api/ofertas?estado=${encodeURIComponent(estado.filtro)}`
              + `&mercado=${encodeURIComponent(estado.mercado)}`
              + `&zona=${encodeURIComponent(estado.zona)}`
              + `&relevancia=${encodeURIComponent(estado.relevancia)}`;
    const data = await pedir(url);
    if (turno !== turnos.ofertas) return;   // ya se pidió otra pantalla
    estado.ofertas = data.ofertas;
    estado.recorte = data.hay_mas
      ? { enseñadas: data.anuncios, totales: data.anuncios_totales }
      : null;
    pintarRejilla();
  } catch (err) {
    // Y AQUÍ TAMPOCO UN AVISO QUE SE BORRA. Si esto falla, la rejilla se queda
    // vacía igualmente, así que un aviso de cuatro segundos deja una pantalla
    // en blanco sin explicación — que es exactamente el primer arranque de
    // antes. La pantalla persistente dice qué pasa y qué hacer.
    if (turno === turnos.ofertas) pantallaDeArranque(err);
  } finally {
    if (turno === turnos.ofertas) $('#cargando').hidden = true;
  }
}

/* ---------- rejilla ---------- */

/* LO QUE NO CABE EN LA PANTALLA, DICHO.

   La bandeja siempre ha traído un máximo de publicaciones por vista y nunca lo
   dijo. Medido el 3 septiembre 2026 con el filtro que sale al abrir la app:
   379 anuncios en la base, 200 llegaban a la pantalla, y de las 307 vacantes
   que ella tenía se veían 168. **139 vacantes no existían**, sin una línea que
   lo dijera. Es la regla nº 1 de la casa rota justo donde trabaja.

   El aviso dice el número y para qué sirven los filtros, porque un aviso que
   solo informa de una pérdida y no da salida es peor que no tenerlo. */
function pintarRecorte() {
  const caja = $('#recorte');
  if (!caja) return;
  if (!estado.recorte) { caja.hidden = true; return; }
  const { enseñadas, totales } = estado.recorte;
  caja.textContent =
    `Se enseñan los ${enseñadas} anuncios más importantes de ${totales}. `
    + 'Las demás siguen en la base: acota con los filtros de arriba '
    + '—mercado, zona o la lectura del sistema— para llegar a ellas.';
  caja.hidden = false;
}

function pintarRejilla() {
  const rejilla = $('#rejilla');
  const vacio = $('#vacio');
  rejilla.innerHTML = '';

  if (estado.ofertas.length === 0) {
    vacio.hidden = false;
    const textos = TEXTOS_VACIO[estado.filtro];
    const hayFiltros = estado.mercado !== 'todos' || estado.zona !== 'todas'
                    || estado.relevancia !== 'todas';
    $('.vacio-texto').textContent =
      textos ? (hayFiltros ? textos.filtrado : textos.limpio) : '';
    pintarRecorte();
    return;
  }
  vacio.hidden = true;
  pintarRecorte();

  estado.ofertas.forEach((o, i) => {
    const card = el('article', 'tarjeta');
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    card.setAttribute('aria-label', `Ver detalle de ${o.title}`);
    card.dataset.indice = i;

    const v = veredictoDe(o);
    card.classList.add(`v-${v}`);

    const alto = el('div', 't-alto');
    alto.append(el('h2', 't-titulo', o.title),
                el('span', 't-fecha', fechaCorta(o.posted_at)));

    const lugar = el('div', 't-lugar');
    lugar.innerHTML = ICONO_LUGAR;   // sin datos externos: solo el icono
    lugar.append(document.createTextNode(o.location_raw || o.location_city || '—'));

    card.append(alto,
                el('p', 't-empresa', o.company || 'Empresa no indicada'),
                lugar);

    // La razón va ANTES del extracto del anuncio: es lo que ella necesita
    // para decidir de un vistazo, y el extracto es solo contexto.
    //
    // Y si no hay razón porque nadie la leyó, va lo que dijo el triaje. Sin
    // esto, una oferta aplazada se vería igual que una que nadie miró.
    if (o.clas_razon) {
      card.append(el('p', 't-razon', o.clas_razon));
    } else if (o.triaje_por_que) {
      const linea = el('div', 't-triaje');
      linea.append(el('span', 't-triaje-puesto', `nº ${o.triaje_posicion}`),
                   el('span', 't-triaje-texto', o.triaje_por_que));
      card.append(linea);
    }
    // Y si no hay ninguna de las dos, la tarjeta NO cae al texto crudo del
    // anuncio. Caía, y eran tres líneas en el idioma del anuncio cortadas a
    // media palabra: lo mismo para una oferta buena que para una mala, así que
    // no ayudaba a decidir y hacía que el sistema pareciera no haber hecho
    // nada. Que no haya nada escrito ES la información —«Nadie la ha mirado»
    // ya sale abajo, en el pie— y decirlo en blanco es más honesto que
    // rellenar el hueco con texto ajeno.

    const pie = el('div', 't-pie');
    if (o.status === 'interesa') pie.append(el('span', 'etiqueta si', 'Interesa'));
    if (o.status === 'descartada') pie.append(el('span', 'etiqueta no', 'Descartada'));

    // «Nadie la ha mirado» solo se dice cuando hay con qué contrastarlo: si el
    // triaje no ha corrido nunca, todas están igual y la etiqueta sería ruido.
    if (v !== 'pendiente' || estado.hayTriaje) {
      pie.append(el('span', `etiqueta ver-${v}`, ETIQUETA_VEREDICTO[v]));
    }
    if (o.clas_cv_id) pie.append(el('span', 'etiqueta cv', o.clas_cv_id));

    const g = o.grupo || {};
    if (g.tamano > 1) {
      pie.append(el('span', 'etiqueta grupo', `${g.tamano} anuncios`));
    }
    if (o.clas_es_reclutadora) pie.append(el('span', 'etiqueta agencia', 'Agencia'));

    pie.append(el('span', 'etiqueta mercado', o.mercado_etiqueta));

    const sal = salarioTexto(o);
    if (sal) pie.append(el('span', 'etiqueta salario', sal));

    card.append(pie);
    rejilla.append(card);
  });
}

/* ---------- modal ---------- */

function abrirModal(indice) {
  const o = estado.ofertas[indice];
  if (!o) return;

  estado.abierta = o;
  estado.focoPrevio = document.activeElement;

  const marcas = $('#modal-marcas');
  marcas.innerHTML = '';
  if (o.status === 'interesa') marcas.append(el('span', 'etiqueta si', 'Interesa'));
  if (o.status === 'descartada') marcas.append(el('span', 'etiqueta no', 'Descartada'));
  marcas.append(el('span', 'etiqueta mercado', o.mercado_etiqueta));

  $('#modal-titulo').textContent = o.title;
  $('#modal-empresa').textContent = o.company || 'Empresa no indicada';

  pintarTriaje(o);
  pintarVeredicto(o);
  pintarGrupo(o);

  const datos = $('#modal-datos');
  datos.innerHTML = '';
  const campos = [
    ['Ubicación', o.location_raw || o.location_city || '—', false],
    ['Publicada', fechaCorta(o.posted_at), true],
    ['Fuente', (o.fuentes || []).join(', ') || '—', false],
  ];
  const sal = salarioTexto(o);
  if (sal) campos.push(['Salario', sal, true]);

  for (const [etiqueta, valor, esNum] of campos) {
    const bloque = el('div', 'dato');
    bloque.append(el('dt', null, etiqueta),
                  el('dd', esNum ? 'num' : null, valor));
    datos.append(bloque);
  }

  const nota = $('#modal-nota');
  // El aviso de la unidad va DELANTE del de la estimación: uno dice que la
  // cifra puede estar equivocada por mil, el otro que es aproximada. No es
  // el mismo tamaño de problema. Ver `sueldoSinUnidad`.
  if (sueldoSinUnidad(o) && sal) {
    nota.textContent =
      'La fuente da este sueldo sin decir si es al año o por hora, y con estas '
      + 'cifras podría ser cualquiera de las dos. Míralo en el anuncio antes '
      + 'de fiarte.';
    nota.hidden = false;
  } else if (o.salary_is_predicted && sal) {
    nota.textContent = 'Este salario es una estimación de la fuente, no viene del anuncio.';
    nota.hidden = false;
  } else {
    nota.hidden = true;
  }

  pintarAnuncio(o);

  /* LOS TRES ESTADOS, SIEMPRE VISIBLES, Y EL SEXTO BOTÓN NO CUESTA NADA.

     Se probó primero a turnarlos —«Ya me postulé» solo cuando la oferta ya
     estuviera en «Interesan»— con el argumento de que el pie no aguantaba seis
     botones. **La medición lo desmintió.** A 1280×620, con la ficha abierta:

         sin veredicto, como estaba antes ....... 2 filas, 180 px
         con veredicto, como estaba antes ....... 2 filas, 185 px
         con los DOS botones, con veredicto ..... 2 filas, 185 px
         con los DOS botones, sin veredicto ..... 2 filas, 180 px

     El pie YA iba en dos filas antes de este cambio, y el motivo no es el
     número de botones: es el `margin-left: auto` de «Abrir en la fuente», que
     se come 57 px de hueco libre y empuja a «Buscar el original» abajo. Añadir
     el sexto botón no mueve ni un píxel.

     Así que se enseñan los tres. Turnarlos habría dado por supuesto un orden
     —primero interesa, después postulada— que nos habríamos inventado
     nosotros, y habría obligado a dos clics y un cambio de pestaña a quien se
     postula directamente sin marcar interés antes. Cuál de las dos formas
     quiere es una pregunta para ella, y va a la reunión con esto funcionando
     delante, que informa mucho más que pedirle que se lo imagine. */
  $('#btn-interesa').classList.toggle('marcado', o.status === 'interesa');
  $('#btn-postulada').classList.toggle('marcado', o.status === 'postulada');
  $('#btn-descartar').classList.toggle('marcado', o.status === 'descartada');

  // EL BOTÓN QUE HACE QUE APLAZAR NO SEA PERDER. Sale en todo lo que no tiene
  // veredicto: lo aplazado por el triaje y lo que nadie ha mirado.
  const leer = $('#btn-leer');
  leer.hidden = o.clas_estado != null;
  leer.disabled = Boolean(estado.trabajo);

  const abrir = $('#btn-abrir');
  abrir.href = o.canonical_url || '#';

  // La salida para cuando ese enlace no lleva a ninguna parte. El porqué está
  // escrito junto al botón, en index.html. Las comillas son deliberadas: sin
  // ellas, un título de tres palabras devuelve medio mercado laboral.
  const consulta = [o.title, o.company]
    .filter(Boolean)
    .map((t) => `"${t}"`)
    .join(' ');
  $('#btn-buscar').href =
    'https://www.google.com/search?q=' + encodeURIComponent(consulta);

  /* EL AVISO DE BLOQUEO, SOLO DONDE BLOQUEA (4 septiembre 2026).
     Salía en todas las fichas. Medido sobre las páginas guardadas y abiertas
     desde México: `adzuna.com` bloquea 247 de 248, y `adzuna.de` cero de 34
     —y 33 de esas 34 traen su botón de postular—. Enseñar la advertencia en
     una ficha alemana es enseñarle a desconfiar del botón justo donde funciona.

     Se decide por el DOMINIO del enlace y no por el mercado a propósito: lo que
     bloquea es una web concreta, y el día que entre otro mercado con índice de
     EE.UU. —o que Adzuna deje de bloquear— el que manda es el dominio. La lista
     se lee como lo que es: «dónde está medido que bloquea». */
  const avisoRegion = $('#pie-nota');
  let dominio = '';
  try { dominio = new URL(abrir.href).hostname; } catch { dominio = ''; }
  const bloquea = DOMINIOS_QUE_BLOQUEAN.some(
    (d) => dominio === d || dominio.endsWith('.' + d),
  );
  avisoRegion.textContent = bloquea
    ? 'La fuente bloquea casi todos estos anuncios cuando se abren desde fuera '
      + 'de EE.UU. Si pasa, «Buscar el original» lo localiza por título y empresa.'
    : '';
  avisoRegion.hidden = !bloquea;

  $('#modal').hidden = false;
  document.body.style.overflow = 'hidden';   // sin scroll detrás del modal
  $('.modal-cerrar').focus();
}

/** El texto del anuncio: plegado, etiquetado y cortado en limpio.
 *
 *  ESTO NO LO ESCRIBE EL SISTEMA, y la ficha no debe dar a entender que sí.
 *  Es el texto crudo del anuncio tal como lo entrega la fuente, y lo entrega
 *  a medias: 500 caracteres exactos en 367 de las 381 ofertas de la base.
 *  Iba suelto al final del modal, en el idioma del anuncio y sin decir de
 *  dónde salía, justo debajo del veredicto en español — que sí es lo que se
 *  ha pagado por producir. Se leía como si el modelo hubiera escrito las dos
 *  cosas y hubiera hecho mal la segunda.
 *
 *  Va plegado porque el orden de la ficha es el orden de lo que decide:
 *  primero el veredicto, luego los datos, y el texto ajeno el último y a un
 *  clic. No se borra porque para las ofertas que nadie ha leído todavía es lo
 *  único del anuncio que ella tiene. */
function pintarAnuncio(o) {
  const caja = $('#modal-desc');

  // LA ETIQUETA TIENE QUE DECIR CUÁL DE LOS DOS TEXTOS ESTÁ VIENDO.
  //
  // Desde el 3 septiembre 2026 hay dos: el recorte de 500 caracteres que
  // devuelve la API, y el anuncio entero traído de la página pública (mediana
  // 5.589 caracteres). Llamar «Ver el anuncio» a los dos sería la regla nº 1
  // rota en la pantalla: con el recorte ella TIENE que abrir la fuente para
  // decidir, y con el entero no — y ese es justo el trabajo que la app le
  // ahorra. Si la etiqueta no los distingue, el ahorro no se nota.
  const entero = (o.description_full || '').trim();
  const texto = entero || extractoLimpio(o.description);

  caja.open = false;
  $('#modal-desc-titulo').textContent = entero
    ? 'Ver el anuncio entero'
    : texto
      ? 'Ver el principio del anuncio'
      : 'La fuente no devolvió el texto del anuncio';
  $('#modal-desc-texto').textContent = texto
    ? texto
    : 'No llegó ninguna descripción con este anuncio. Está en la web de la ' +
      'fuente, con «Abrir en la fuente».';
}

/** Corta en la última palabra entera y marca el corte.
 *
 *  La fuente ya deja el texto a medias y pega sus puntos suspensivos a la
 *  sílaba donde cortó: «…contribute to the banks economic objectives. Th…».
 *  Eso se lee como un fallo del programa y no lo es — es lo que llega—, así
 *  que se retrocede a la última palabra completa. El corte se sigue marcando:
 *  hacerlo desaparecer daría a entender que el anuncio termina ahí, y esconder
 *  que falta texto es la regla nº 1 rota en pequeño. */
function extractoLimpio(texto) {
  let t = (texto || '').trim();
  if (!t) return '';

  const cortado = /(…|\.\.\.)$/.test(t);
  t = t.replace(/(…|\.\.\.)$/, '').trimEnd();
  if (!cortado) return t;

  const corte = t.lastIndexOf(' ');
  if (corte > 40) t = t.slice(0, corte).trimEnd();
  return t.replace(/[,;:.]$/, '') + '…';
}

/** Lo que dijo el triaje: su puesto y su motivo.
 *
 *  Es la diferencia entre «el asistente la vio y la dejó en el puesto 34,
 *  porque X» y «nadie ha mirado nunca esta oferta». Sin esta caja, la segunda
 *  frase es la única que la interfaz sabe decir. */
function pintarTriaje(o) {
  const caja = $('#modal-triaje');

  // Solo si el triaje la vio Y todavía no hay veredicto. En cuanto se
  // clasifica, lo que manda es la razón de haberla leído entera: repetir aquí
  // la impresión que dio el título sería enseñar dos opiniones y no decir
  // cuál vale.
  if (o.triaje_posicion == null || o.clas_estado != null) {
    caja.hidden = true;
    return;
  }
  caja.hidden = false;

  const cuando = o.triaje_at ? ` · ${fechaCorta(o.triaje_at)}` : '';
  $('#triaje-puesto').textContent = `puesto ${o.triaje_posicion} del triaje${cuando}`;
  $('#triaje-porque').textContent = o.triaje_por_que || 'Sin motivo registrado.';
}

/** El veredicto de la capa 2, con su razón a la vista. */
function pintarVeredicto(o) {
  const caja = $('#modal-veredicto');
  const v = veredictoDe(o);

  if (v === 'pendiente' || v === 'aplazada') { caja.hidden = true; return; }
  caja.hidden = false;

  const marca = $('#veredicto-marca');
  marca.textContent = ETIQUETA_VEREDICTO[v];
  marca.className = `veredicto-marca ver-${v}`;

  $('#veredicto-confianza').textContent =
    o.clas_confianza != null ? `confianza ${o.clas_confianza.toFixed(2)}` : '';

  $('#veredicto-razon').textContent =
    o.clas_razon || o.clas_error || 'Sin razón registrada.';

  const duda = $('#veredicto-duda');
  let texto = o.clas_motivo_criterio || (!o.clas_relevante ? o.clas_motivo_descarte : null);
  // LA MISMA FRASE, DOS VECES. Cuando la lectura falla, el motivo se guarda a
  // la vez en `clas_error` y en `clas_motivo_criterio`: arriba salía como
  // razón y aquí debajo otra vez, palabra por palabra (medido el 9 septiembre
  // 2026 con la cuenta sin saldo). Repetir no añade nada y hace dudar de si
  // son dos problemas distintos.
  if (texto && texto === $('#veredicto-razon').textContent) texto = null;
  duda.textContent = texto || '';
  duda.hidden = !texto;

  const datos = $('#veredicto-datos');
  datos.innerHTML = '';
  const campos = [];
  if (o.clas_cv_id) campos.push(['CV a adjuntar', o.clas_cv_id]);
  if (o.clas_seniority) campos.push(['Nivel', NIVEL[o.clas_seniority] || o.clas_seniority]);
  if (o.clas_modalidad) campos.push(['Modalidad', MODALIDAD[o.clas_modalidad] || o.clas_modalidad]);
  if (o.clas_es_reclutadora) campos.push(['Publica', 'Una agencia, no el empleador']);

  for (const [etiqueta, valor] of campos) {
    const bloque = el('div', 'dato');
    bloque.append(el('dt', null, etiqueta), el('dd', null, valor));
    datos.append(bloque);
  }
}

/** Los otros anuncios de la misma vacante. */
function pintarGrupo(o) {
  const caja = $('#modal-grupo');
  const g = o.grupo || {};

  if (!g.tamano || g.tamano < 2) { caja.hidden = true; return; }
  caja.hidden = false;

  $('#grupo-titulo').textContent = g.resumen;
  $('#grupo-motivo').textContent = g.motivos && g.motivos.length
    ? `Agrupadas porque: ${g.motivos.join('; ')}.`
    : '';

  const lista = $('#grupo-lista');
  lista.innerHTML = '';
  for (const m of g.miembros) {
    const li = el('li');
    li.append(el('span', 'grupo-lugar', m.ubicacion || 'Sin ubicación'));
    if (m.url) {
      const a = el('a', 'grupo-enlace', 'abrir');
      a.href = m.url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      li.append(a);
    }
    li.append(el('span', 'grupo-titulo-item', m.titulo));
    lista.append(li);
  }
}

function cerrarModal() {
  $('#modal').hidden = true;
  document.body.style.overflow = '';
  estado.abierta = null;
  if (estado.focoPrevio) estado.focoPrevio.focus();
}

/* ---------- acciones sobre una oferta ---------- */

async function marcar(nuevoEstado) {
  const o = estado.abierta;
  if (!o) return;

  // LA DECISIÓN ES DE LA VACANTE, NO DEL ANUNCIO QUE HACE DE PORTADA.
  //
  // La ficha acaba de decirle «la misma vacante aparece 4 veces» y le ha
  // enseñado las cuatro. Si solo se marcara `o.dedupe_key`, las otras tres
  // seguirían en «nueva» y la tarjeta volvería en el siguiente refresco con
  // un anuncio menos — medido el 9 septiembre 2026 sobre una vacante de diez.
  //
  // Se mandan las claves DE ESTA FICHA y no se recalculan en el servidor: el
  // grupo depende de los filtros con los que se pintó, y solo el cliente los
  // conoce. Ver `CambioEstado.hermanas`.
  const hermanas = (o.grupo && o.grupo.miembros ? o.grupo.miembros : [])
    .map((m) => m.clave)
    .filter(Boolean);

  try {
    await pedir(`/api/ofertas/${o.dedupe_key}/estado`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ estado: nuevoEstado, hermanas }),
    });
  } catch (err) {
    avisar(`No se pudo guardar: ${err.message}`);
    return;
  }

  avisar({
    interesa: 'Guardada en «Interesan»',
    postulada: 'Anotada como enviada. Sale del orden del asistente.',
    descartada: 'Oferta descartada',
  }[nuevoEstado] || 'Actualizada');

  o.status = nuevoEstado;
  cerrarModal();

  // Si ya no encaja en el filtro que se está viendo, desaparece de la rejilla.
  if (estado.filtro !== nuevoEstado) {
    estado.ofertas = estado.ofertas.filter((x) => x.dedupe_key !== o.dedupe_key);
  }

  pintarRejilla();
  cargarResumen();
}

async function cambiarFiltro(nuevo) {
  estado.filtro = nuevo;
  document.querySelectorAll('.filtros:not(.filtros-rel):not(.filtros-zona) .filtro').forEach((f) => {
    const activo = f.dataset.estado === nuevo;
    f.classList.toggle('activo', activo);
    f.setAttribute('aria-pressed', String(activo));
  });
  // LOS CONTADORES TAMBIÉN, porque cuentan bajo el estado elegido: la fila de
  // zonas y la de la lectura del sistema describían el estado anterior hasta
  // que otra cosa pedía el resumen.
  await Promise.all([cargarOfertas(), cargarResumen()]);
}

/* =========================================================================
   EL PRESUPUESTO
   ========================================================================= */

async function cargarPresupuesto() {
  let p;
  try {
    p = await pedir('/api/presupuesto');
  } catch (err) {
    $('#presupuesto-texto').textContent = 'presupuesto: ?';
    estado.presupuesto = null;
    return;
  }

  // Se guarda para que el diálogo de confirmación pueda decir si el mes FRENA
  // o solo AVISA. Sin esto tendría que elegir a ciegas entre dos frases que
  // describen comportamientos distintos del servidor.
  estado.presupuesto = p;

  const caja = $('#presupuesto');

  // UN CONTADOR, NO UNA BARRA DE PROGRESO (8 septiembre 2026).
  //
  // Aquí ponía «$4,47 de $12,00 este mes», con barra de relleno, ámbar al 75%
  // y rojo al agotarse. Ese denominador no era un límite de nada: el mes solo
  // avisa, y desde que la cuenta de Anthropic se recarga sola al bajar de un
  // umbral tampoco hay un techo detrás. Enseñar un tope que no frena —y
  // pintarlo de rojo— es prometer una protección que no existe, que es peor
  // que no enseñar nada.
  //
  // Lo que sí frena es el tope POR ACCIÓN, y eso vive en el título de abajo y
  // en cada diálogo antes de cobrar. Aquí queda lo que ella pidió ver: cuánto
  // lleva gastado, de qué mes, y cuándo vuelve a cero. Con eso decide ella.
  //
  // EL DENOMINADOR VUELVE SI ALGÚN DÍA FRENA DE VERDAD. `frena_el_mes` es
  // configurable (`al_agotarse: parar`), y un límite que corta y no se ve
  // sería el mismo fallo del revés.
  const frena = p.suyo.frena_el_mes === true;
  caja.classList.toggle('avisa', frena && p.conviene_avisar && !p.suyo.agotado);
  caja.classList.toggle('agotado', frena && p.suyo.agotado);

  $('#presupuesto-texto').textContent = frena
    ? `$${p.gastado_mes.toFixed(2)} de $${p.techo_mensual.toFixed(2)} este mes`
    : `$${p.gastado_mes.toFixed(2)} gastados en ${p.mes_nombre}`;

  caja.title =
    `Gastado en ${p.mes_nombre}: $${p.gastado_mes.toFixed(4)} en ` +
    `${p.llamadas_mes} llamadas.\n` +
    `Vuelve a cero ${p.reinicio}. Es una cuenta, no un límite.\n` +
    `Ningún botón puede costar más de $${p.tope_por_accion.toFixed(2)} de una ` +
    `vez. Eso es lo que frena.` +
    (frena ? `\nEl mes se para en $${p.techo_mensual.toFixed(2)}.` : '') +
    // AQUÍ DECÍA «Las corridas automáticas sí se paran, en $10.00 al mes».
    //
    // Es verdad del mecanismo y mentira de su máquina: el 9 septiembre 2026
    // se comprobó que `instalar.ps1` no registra ninguna tarea programada y
    // que no hay ninguna en el equipo. O sea que le explicábamos el freno de
    // unas corridas que no ocurren, en el único sitio donde mira cuánto
    // lleva gastado — y todo lo que se le dice del dinero tiene que poder
    // comprobarlo.
    //
    // La ración sigue existiendo en el código y en `search_profile.yaml`
    // (`racion_automatica_mensual_usd`) para el día que haya tarea. Ese día
    // esta línea vuelve, y no antes.
    '';
}

/* =========================================================================
   POR DÓNDE EMPEZAR
   ========================================================================= */

async function cargarRanking() {
  let r;
  try {
    r = await pedir('/api/ranking');
  } catch (_) {
    return;
  }

  const caja = $('#ranking');
  if (!r.hay) { caja.hidden = true; return; }
  caja.hidden = false;

  // El «de N» solo sale cuando el orden se recortó. Un ranking que las miró
  // todas no necesita coletilla, y ponerla siempre la convertiría en ruido
  // justo donde tiene que significar algo.
  const fuera = (r.ofertas_disponibles || 0) - r.ofertas_consideradas;
  $('#ranking-meta').textContent =
    (fuera > 0
      ? `${r.ofertas_consideradas} de ${r.ofertas_disponibles} ofertas`
      : `${r.ofertas_consideradas} ofertas`) +
    ` · ${fechaCorta(r.creado_at)} · ${dolares(r.coste_usd)}`;

  // EL AVISO USA `recortadas`, NO ESTA RESTA. Son dos números parecidos y
  // distintos, y cada uno tiene su sitio:
  //
  //   `20 de 45`   arriba, es el HECHO HISTÓRICO: lo que este orden hizo el
  //                día que se hizo. No cambia nunca.
  //   `recortadas` aquí, es lo que sigue SIN ORDENAR ahora mismo, y baja
  //                según ella va revisando. Es lo que la frase promete:
  //                «que tienes sin revisar».
  //
  // Calcularlo aquí con una resta daría un tercer número que no describe ni
  // una cosa ni la otra en cuanto ella marque una oferta.
  const recorte = $('#ranking-recorte');
  const sinOrdenar = (r.desajuste || {}).recortadas || 0;
  if (recorte) {
    recorte.hidden = sinOrdenar <= 0;
    recorte.textContent = sinOrdenar > 0
      ? `Este orden no mira ${sinOrdenar} oferta${sinOrdenar === 1 ? '' : 's'} ` +
        `más antigua${sinOrdenar === 1 ? '' : 's'} que tienes sin revisar. ` +
        `No se ha${sinOrdenar === 1 ? '' : 'n'} perdido: ` +
        `est${sinOrdenar === 1 ? 'á' : 'án'} en la bandeja, aquí debajo.`
      : '';
  }
  $('#ranking-lectura').textContent = r.lectura;

  pintarDesajusteDelRanking(r.desajuste);

  const patrones = $('#ranking-patrones');
  patrones.innerHTML = '';
  for (const p of r.patrones || []) patrones.append(el('li', null, p));

  const lista = $('#ranking-lista');
  lista.innerHTML = '';
  for (const item of r.items) {
    const li = el('li', `ranking-item accion-${item.accion}`);
    li.dataset.clave = item.clave;

    const nombre = el('div', 'ranking-nombre', item.titulo);
    nombre.append(el('span', 'ranking-empresa', `  ·  ${item.empresa}`));

    li.append(el('span', 'ranking-puesto', String(item.posicion)),
              nombre,
              el('div', 'ranking-porque', item.por_que));
    lista.append(li);
  }

  // Se aplica DESPUÉS de pintar las líneas, porque el botón dice cuántas son.
  pintarPliegue(pliegueRecordado());
}

/* CUÁNTO HA ENVEJECIDO EL ORDEN, DICHO EN LA PANTALLA.

   El ranking es lo único del sistema que se lee entero, y hasta hoy lo único
   que decía de sí mismo era su fecha. «Hoy» tranquiliza y no significa nada:
   sobre la base del 3 septiembre 2026 el orden de las 09:20 enseñaba 49
   ofertas de las que **11 ya estaban descartadas** por el propio sistema.

   Se dicen las tres cosas por separado porque no son la misma:
   lo que falta, lo que sobra, y lo que ella ya decidió — que no es un fallo
   del orden, es que va por delante. */
function pintarDesajusteDelRanking(d) {
  const caja = $('#ranking-viejo');
  if (!d || !(d.nuevas || d.ya_no_encajan)) { caja.hidden = true; return; }

  const partes = [];
  if (d.ya_no_encajan) {
    partes.push(
      `${d.ya_no_encajan} de las ${d.total} de esta lista ya no encajan: el ` +
      `asistente las volvió a leer y las descartó después de ordenar`);
  }
  if (d.nuevas) {
    partes.push(
      `${d.nuevas} oferta${d.nuevas === 1 ? '' : 's'} clasificada` +
      `${d.nuevas === 1 ? '' : 's'} después no está${d.nuevas === 1 ? '' : 'n'} ` +
      `en este orden`);
  }
  if (d.ya_decididas) {
    partes.push(`${d.ya_decididas} ya las marcaste tú`);
  }

  $('#ranking-viejo-texto').textContent =
    'Este orden se ha quedado atrás: ' + partes.join('; ') + '.';
  caja.hidden = false;
}

/* =========================================================================
   CONFIRMAR ANTES DE GASTAR

   La regla de la casa hecha pantalla. Nada de lo que cuesta dinero o cuota se
   lanza sin pasar por aquí.
   ========================================================================= */

function abrirConfirmar(plan, alAceptar) {
  $('#confirmar-titulo').textContent = plan.etiqueta;

  const enDolares = plan.moneda === 'usd';
  $('#confirmar-valor').textContent = enDolares
    ? dolares(plan.coste_usd)
    : String(plan.peticiones);
  $('#confirmar-unidad').textContent = enDolares
    ? 'USD estimados'
    : 'peticiones a los portales (no cuestan dinero)';

  $('#confirmar-nota').textContent = plan.nota || '';

  const datos = $('#confirmar-datos');
  datos.innerHTML = '';
  for (const [etiqueta, valor] of Object.entries(plan.detalle || {})) {
    datos.append(el('dt', null, etiqueta), el('dd', null, String(valor)));
  }
  // SOLO SI EL MES FRENA DE VERDAD (8 septiembre 2026).
  //
  // Esta fila decía «Margen hasta la referencia mensual de gasto» cuando el
  // mes solo avisa, o sea: un margen contra un límite que no corta, sacado
  // además del libro LOCAL y no del saldo de Anthropic. Era el mismo
  // denominador inventado que se ha quitado de la cabecera, escondido en un
  // diálogo que habla de dinero. Cuando `al_agotarse: parar`, la cifra sí
  // describe algo que va a pasar, y entonces se enseña.
  const presu = estado.presupuesto;
  const frenaElMes = (presu && presu.suyo) ? presu.suyo.frena_el_mes : null;
  if (enDolares && plan.disponible_usd != null && frenaElMes === true) {
    datos.append(
      el('dt', null, 'Queda del techo del mes'),
      el('dd', null, dolares(plan.disponible_usd)));
  }

  const avisos = $('#confirmar-avisos');
  avisos.innerHTML = '';
  const lista = [...(plan.avisos || [])];
  if (!plan.hay_trabajo) lista.push(plan.nota);
  for (const texto of lista) avisos.append(el('div', 'confirmar-aviso', texto));
  if (enDolares && !plan.cabe && frenaElMes === true) {
    // SOLO CUANDO EL SERVIDOR SE VA A NEGAR (8 septiembre 2026).
    //
    // Con el mes en modo FRENO, `_verificar_que_cabe` RECHAZA el POST y no
    // empieza nada: eso hay que decirlo antes de que pulse. Con el mes en modo
    // AVISO —que es como está— el servidor ni mira esta cifra y la corrida se
    // lanza entera, así que aquí salía una advertencia roja sobre un límite
    // que no iba a pasar nada. Una alarma que no describe ningún riesgo es la
    // que hace que se ignoren las que sí.
    //
    // Lo que puede impedir la corrida es el tope POR ACCIÓN, y de eso se
    // encarga `_verificar_que_cabe` con su propio mensaje.
    avisos.append(el('div', 'confirmar-aviso malo',
      'La estimación supera lo que queda del techo del mes, así que no se ' +
      'iniciará. No se pierde ninguna oferta: siguen pendientes.'));
  }
  avisos.hidden = avisos.children.length === 0;

  const muestra = $('#confirmar-muestra');
  muestra.innerHTML = '';
  for (const m of plan.muestra || []) {
    const li = el('li');
    li.append(el('b', null, m.titulo), document.createTextNode(`  ·  ${m.empresa}`));
    muestra.append(li);
  }
  muestra.hidden = muestra.children.length === 0;

  // Sin trabajo que hacer no hay nada que confirmar: se enseña el porqué y el
  // único botón útil es el de cerrar.
  $('#confirmar-si').disabled = !plan.hay_trabajo;
  estado.confirmando = alAceptar;

  $('#confirmar').hidden = false;
  document.body.style.overflow = 'hidden';
  $('#confirmar-si').focus();
}

function cerrarConfirmar() {
  $('#confirmar').hidden = true;
  estado.confirmando = null;
  if ($('#modal').hidden) document.body.style.overflow = '';
}

async function pedirYConfirmar(accion, clave) {
  if (estado.trabajo) {
    avisar('Ya hay una corrida en marcha. Espera a que acabe.');
    return;
  }
  let plan;
  try {
    plan = clave
      ? await pedir(`/api/ofertas/${clave}/plan`)
      : await pedir(`/api/plan/${accion}`);
  } catch (err) {
    avisar(err.message);
    return;
  }

  abrirConfirmar(plan, () => lanzar(accion, clave, plan.coste_usd));
}

/* =========================================================================
   LOS TRABAJOS EN SEGUNDO PLANO
   ========================================================================= */

const FINALES = {
  terminado:   'Listo.',
  con_errores: 'Terminó, pero algo quedó sin hacer. Mira la salida.',
  // NO es un fallo, y por eso no se pinta de rojo: lo que no se hizo sigue
  // pendiente y se hará solo en cuanto haya saldo. Decirlo como un error haría
  // creer que esas ofertas se perdieron.
  sin_saldo:   'Se paró al llegar al techo del mes. Lo que falta sigue ' +
               'pendiente y no se ha perdido ninguna oferta.',
  // NO «mira la salida»: eso le encarga interpretar un registro técnico. El
  // botón «Si algo va mal» ya copia ese texto y abre la carpeta, y es el mismo
  // camino al que `avisar` manda los errores de red (ver el pie del archivo).
  fallido:     'No se pudo completar el trabajo. Pulsa «Si algo va mal», abajo ' +
               'a la izquierda, y envía a Juan Pablo el texto copiado y los ' +
               'archivos de la carpeta.',
  cancelado:   'Parado. Lo que ya se había hecho se queda hecho.',
};

async function lanzar(accion, clave, costeConfirmado) {
  cerrarConfirmar();

  let trabajo;
  try {
    trabajo = await pedir('/api/trabajos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        accion,
        clave: clave || null,
        coste_confirmado_usd: costeConfirmado,
      }),
    });
  } catch (err) {
    avisar(err.message);
    return;
  }

  if (!$('#modal').hidden) cerrarModal();
  empezarASeguir(trabajo);
}

function empezarASeguir(trabajo) {
  estado.trabajo = trabajo;
  pintarTrabajo(trabajo);
  botonesActivos(false);

  clearInterval(estado.reloj);
  estado.reloj = setInterval(async () => {
    let al_dia;
    try {
      al_dia = await pedir(`/api/trabajos/${trabajo.id}`);
    } catch (_) {
      // Un fallo suelto de red no tiene por qué matar el seguimiento; el
      // siguiente tic lo vuelve a intentar.
      return;
    }
    pintarTrabajo(al_dia);
    if (al_dia.terminado) terminarDeSeguir(al_dia);
  }, 1200);
}

async function terminarDeSeguir(trabajo) {
  clearInterval(estado.reloj);
  estado.reloj = null;
  estado.trabajo = null;
  botonesActivos(true);

  // Se recarga TODO: una corrida puede haber metido ofertas, clasificado,
  // gastado dinero y dejado un ranking nuevo.
  await Promise.all([cargarResumen(), cargarPresupuesto(), cargarRanking()]);
  cargarUltimaCorrida();
  await cargarOfertas();

  if (trabajo.accion === 'leer_una' && trabajo.estado === 'terminado') {
    avisar('Leída. Su veredicto ya está en la bandeja.');
  }
}

function pintarTrabajo(t) {
  const caja = $('#trabajo');
  caja.hidden = false;

  $('#trabajo-etiqueta').textContent = t.etiqueta;

  // «paso 2 de 3 · Que el asistente lea lo nuevo». Solo en las cadenas: en un
  // trabajo de un paso diría «paso 1 de 1», que es ruido.
  const paso = $('#trabajo-paso');
  const enCadena = (t.pasos_total || 1) > 1 && t.paso_actual > 0;
  paso.hidden = !enCadena;
  if (enCadena) {
    paso.textContent = `paso ${t.paso_actual} de ${t.pasos_total}` +
      (t.paso_etiqueta ? ` · ${t.paso_etiqueta}` : '');
  }

  $('#trabajo-spinner').hidden = t.terminado;
  $('#trabajo-cerrar').hidden = !t.terminado;
  $('#trabajo-parar').hidden = t.terminado;

  const barra = $('#trabajo-barra');
  if (t.total > 0) {
    barra.hidden = false;
    $('#trabajo-relleno').style.width = `${Math.round((t.hechos / t.total) * 100)}%`;
  } else {
    barra.hidden = true;
  }

  const lineas = t.lineas || [];
  const ultima = [...lineas].reverse().find((l) => l.trim() && !l.startsWith('==='));
  $('#trabajo-linea').textContent = t.terminado
    ? (t.total ? `${t.hechos} de ${t.total}` : '')
    : (ultima || 'Arrancando…');

  const final = $('#trabajo-final');
  // LO QUE QUEDÓ SIN HACER MANDA SOBRE EL «Listo».
  //
  // Una cadena que se para porque el siguiente paso no cabía en el tope NO ha
  // fallado: ha cumplido la promesa. Pero decir «Listo.» ahí sería decirle que
  // está al día cuando le falta el paso que más lee —el orden—, y eso es la
  // regla nº 1 de la casa rota justo donde se nota. El motivo viene redactado
  // del servidor, con el importe dentro.
  final.textContent = t.terminado
    ? (t.quedo_sin_hacer || FINALES[t.estado] || t.estado)
    : '';
  final.className = `trabajo-final ${t.quedo_sin_hacer ? 'con_errores' : t.estado}`;
  final.hidden = !t.terminado;

  $('#trabajo-salida').textContent = lineas.join('\n');
  const salida = $('#trabajo-salida');
  if (!salida.hidden) salida.scrollTop = salida.scrollHeight;
}

function botonesActivos(activos) {
  document.querySelectorAll('.btn-accion').forEach((b) => { b.disabled = !activos; });
  // `#btn-rehacer` no lleva la clase `btn-accion` a propósito —si la llevara,
  // el `forEach` de más abajo le engancharía un segundo escuchador y un clic
  // abriría dos confirmaciones—, así que se apaga aquí a mano. Sin esto se
  // quedaría encendido durante una corrida, y aunque `pedirYConfirmar` lo
  // rechaza igual, un botón que se puede pulsar y no hace nada es peor que uno
  // apagado que explica por qué.
  for (const id of ['#btn-leer', '#btn-rehacer']) {
    const boton = $(id);
    if (boton) boton.disabled = !activos;
  }
}

async function pararTrabajo() {
  if (!estado.trabajo) return;
  try {
    await pedir(`/api/trabajos/${estado.trabajo.id}/cancelar`, { method: 'POST' });
  } catch (err) {
    avisar(err.message);
  }
}

/* ---------- eventos ---------- */

document.querySelectorAll('.filtros:not(.filtros-rel):not(.filtros-zona) .filtro').forEach((f) => {
  f.addEventListener('click', () => cambiarFiltro(f.dataset.estado));
});

document.querySelectorAll('.filtros-rel .filtro').forEach((f) => {
  f.addEventListener('click', async () => {
    estado.relevancia = f.dataset.rel;
    document.querySelectorAll('.filtros-rel .filtro').forEach((otro) => {
      const activo = otro.dataset.rel === estado.relevancia;
      otro.classList.toggle('activo', activo);
      otro.setAttribute('aria-pressed', String(activo));
    });
    // Las pastillas cuentan lo del filtro puesto, así que al cambiarlo hay
    // que volver a pedirlas: si no, la fila de abajo describiría la pantalla
    // anterior, que es peor que no decir nada.
    await Promise.all([cargarOfertas(), cargarResumen()]);
  });
});

$('#mercado').addEventListener('change', (e) => {
  estado.mercado = e.target.value;
  // Una zona es de un mercado: al cambiar de mercado, se vuelve a «Todas».
  estado.zona = 'todas';
  cargarOfertas();
  cargarResumen();
});

// Los botones de zona se crean en cada resumen, así que se escucha en la fila.
$('#filtros-zona').addEventListener('click', async (e) => {
  const boton = e.target.closest('.filtro');
  if (!boton || boton.dataset.zona === estado.zona) return;
  estado.zona = boton.dataset.zona;
  document.querySelectorAll('#filtros-zona .filtro').forEach((otro) => {
    const activo = otro.dataset.zona === estado.zona;
    otro.classList.toggle('activo', activo);
    otro.setAttribute('aria-pressed', String(activo));
  });
  await Promise.all([cargarOfertas(), cargarResumen()]);
});

// Un solo escuchador en la rejilla en lugar de uno por tarjeta.
$('#rejilla').addEventListener('click', (e) => {
  const card = e.target.closest('.tarjeta');
  if (card) abrirModal(Number(card.dataset.indice));
});

$('#rejilla').addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const card = e.target.closest('.tarjeta');
  if (card) {
    e.preventDefault();
    abrirModal(Number(card.dataset.indice));
  }
});

document.querySelectorAll('[data-cerrar]').forEach((n) => {
  n.addEventListener('click', cerrarModal);
});

document.querySelectorAll('[data-cerrar-confirmar]').forEach((n) => {
  n.addEventListener('click', cerrarConfirmar);
});

$('#confirmar-si').addEventListener('click', () => {
  if (estado.confirmando) estado.confirmando();
});

$('#btn-interesa').addEventListener('click', () => marcar('interesa'));
$('#btn-postulada').addEventListener('click', () => marcar('postulada'));
$('#btn-descartar').addEventListener('click', () => marcar('descartada'));

$('#btn-leer').addEventListener('click', () => {
  if (estado.abierta) pedirYConfirmar('leer_una', estado.abierta.dedupe_key);
});

document.querySelectorAll('.btn-accion').forEach((b) => {
  b.addEventListener('click', () => pedirYConfirmar(b.dataset.accion, null));
});

// «Rehacer el orden» pasa por el MISMO camino que el botón de la cabecera:
// se pide el plan, se enseña el número, y solo entonces se lanza. No hay
// ningún atajo que gaste sin confirmar, y este no iba a ser el primero.
$('#btn-rehacer').addEventListener('click', () => pedirYConfirmar('ranking', null));

$('#trabajo-parar').addEventListener('click', pararTrabajo);
$('#trabajo-cerrar').addEventListener('click', () => { $('#trabajo').hidden = true; });
$('#trabajo-detalle').addEventListener('click', () => {
  const salida = $('#trabajo-salida');
  salida.hidden = !salida.hidden;
  $('#trabajo-detalle').textContent = salida.hidden ? 'Ver la salida' : 'Ocultar la salida';
  if (!salida.hidden) salida.scrollTop = salida.scrollHeight;
});

/* SI LO PLIEGA, SE QUEDA PLEGADO. Y el motivo es de sitio, no de gusto.

   Medido el 3 septiembre 2026 con el ranking real: 49 líneas ocupan **3.986
   px** y la primera tarjeta de la bandeja empieza en el píxel **4.243**. O sea
   que para llegar a sus ofertas hay que pasar por delante del orden entero,
   casi cinco pantallas, **cada vez que abre la app**. Y va a peor solo: el lote
   del ranking crece con lo que ella no ha revisado.

   El botón de plegar ya existía; lo que faltaba es que se acordara. Se guarda
   en el navegador —es una preferencia de esta pantalla, no un dato del
   proyecto— y por eso no toca la base ni viaja a ninguna parte.

   NO se pliega solo por ser largo: el orden es lo único del sistema que ella
   lee entero, y esconderlo por nuestra cuenta sería decidir por ella lo que
   lee. Lo que se hace es respetar lo que decida. */
const PLEGADO_DEL_ORDEN = 'job-rob.orden-plegado';

function recordarPliegue(plegado) {
  try {
    localStorage.setItem(PLEGADO_DEL_ORDEN, plegado ? '1' : '0');
  } catch (_) { /* navegador con el almacenamiento capado: no es motivo de nada */ }
}

function pliegueRecordado() {
  try {
    return localStorage.getItem(PLEGADO_DEL_ORDEN) === '1';
  } catch (_) { return false; }
}

function pintarPliegue(plegado) {
  const boton = $('#ranking-plegar');
  const filas = $('#ranking-lista').children.length;
  $('#ranking-cuerpo').hidden = plegado;
  // El número va en el botón porque plegado el panel no dice cuánto esconde, y
  // «Mostrar» a secas no distingue tres líneas de cincuenta.
  boton.textContent = plegado
    ? (filas ? `Mostrar las ${filas}` : 'Mostrar')
    : 'Ocultar';
  boton.setAttribute('aria-expanded', String(!plegado));
}

$('#ranking-plegar').addEventListener('click', () => {
  const plegado = !$('#ranking-cuerpo').hidden;
  pintarPliegue(plegado);
  recordarPliegue(plegado);
});

/* Pinchar una línea del ranking abre esa oferta.

   Y HASTA HOY UN TERCIO DE LAS LÍNEAS NO ABRÍA NADA. Medido el 3 septiembre
   2026 sobre el ranking real, con el filtro que sale al abrir la app:

       líneas del ranking ......................... 49
       las que no abrían al pincharlas ............ 16

   Dos causas distintas, y ninguna se veía desde fuera —el aviso decía siempre
   lo mismo, «no está en el filtro que tienes puesto», que además era falso en
   la mitad de los casos:

   · **8 eran de agrupación.** El ranking guarda la clave del anuncio que se le
     mandó al modelo, y la rejilla indexa por el REPRESENTANTE de la vacante.
     Cuando no son el mismo anuncio —pasa en cuanto una vacante está publicada
     en varias ciudades, que es un tercio de la base—, la búsqueda por clave
     fallaba aunque la tarjeta estuviera ahí delante. Se arregla mirando también
     dentro de `grupo.miembros`, que ya viaja en la respuesta.
   · **8 eran del tope de la vista**, y esas se arreglan solas al decir el
     recorte (ver `pintarRecorte`).

   Es la pieza que ella lee entera: una línea que no responde al clic es la
   forma más rápida de que deje de fiarse de la lista. */
function indiceDeLaTarjeta(clave) {
  const directo = estado.ofertas.findIndex((o) => o.dedupe_key === clave);
  if (directo >= 0) return directo;
  // La misma vacante, publicada en otra ciudad: la tarjeta existe, pero está
  // indexada por el anuncio que se eligió como representante.
  return estado.ofertas.findIndex(
    (o) => (o.grupo?.miembros || []).some((m) => m.clave === clave));
}

$('#ranking-lista').addEventListener('click', (e) => {
  const li = e.target.closest('.ranking-item');
  if (!li) return;
  const indice = indiceDeLaTarjeta(li.dataset.clave);
  if (indice >= 0) {
    abrirModal(indice);
  } else if (estado.recorte) {
    avisar('Esa oferta está fuera de las que caben en esta pantalla. '
         + 'Acota con los filtros de arriba y vuelve a pincharla.');
  } else {
    avisar('Esa oferta no está en el filtro que tienes puesto ahora.');
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (!$('#ayuda').hidden) { cerrarAyuda(); return; }
  if (!$('#confirmar').hidden) { cerrarConfirmar(); return; }
  if (!$('#modal').hidden) cerrarModal();
});

/* ---------- dudas y ayuda ----------

   EL ÚNICO DOCUMENTO ESCRITO PARA ELLA. El README es el cuaderno de
   ingeniería y no viaja a su máquina; lo que viaja es `AYUDA.md`, y se lee
   aquí dentro para que la respuesta esté donde aparece la duda — un archivo
   en una carpeta es un archivo que no se abre.

   VA ANTES DE «SI ALGO VA MAL» A PROPÓSITO. Ese botón prepara un correo para
   otro continente, y la mayoría de las dudas —los 30 segundos del arranque,
   una pantalla vacía por un filtro, un anuncio que no abre desde México— se
   contestan sin escribirle a nadie.

   SE PINTA UN SUBCONJUNTO DE MARKDOWN, Y ES DELIBERADO. `AYUDA.md` usa poco:
   títulos, listas, separadores, negrita y algo de `código`. Traer una
   librería de Markdown serían kilobytes para un archivo de cien líneas que
   escribo yo. (El motivo original era que el archivo también se imprimía en
   papel; se decidió no imprimirlo el 7 sept 2026, pero el subconjunto se
   queda: el que sobra es el intérprete, no el texto.)

   Y SE CONSTRUYE CON NODOS, NO CON `innerHTML`. Aunque el archivo sea mío,
   `innerHTML` convierte en marcado cualquier `<` del texto: el día que una
   duda se conteste con un ejemplo que lleve un signo raro, la pantalla se
   rompería sin decir nada. Con nodos, el texto es texto. */

const AYUDA_INLINE = /(\*\*[^*]+\*\*|`[^`]+`)/g;

/** Mete `texto` en `destino` resolviendo la negrita y el `código`. */
function trozosDeLinea(destino, texto) {
  for (const trozo of texto.split(AYUDA_INLINE)) {
    if (!trozo) continue;
    if (trozo.startsWith('**') && trozo.endsWith('**')) {
      destino.append(el('strong', null, trozo.slice(2, -2)));
    } else if (trozo.startsWith('`') && trozo.endsWith('`')) {
      destino.append(el('code', null, trozo.slice(1, -1)));
    } else {
      destino.append(document.createTextNode(trozo));
    }
  }
  return destino;
}

/* EL FORMATO SE RESUELVE AL CERRAR EL BLOQUE, NO LÍNEA A LÍNEA, y esto salió
   ejecutándolo: `AYUDA.md` está escrito a 79 columnas, así que una negrita
   parte en dos líneas cada dos por tres: «**Tarda unos 30 segundos… / …no
   parece que pase nada.**». Mirando línea a línea, esa primera mitad no tiene
   cierre y los asteriscos se pintaban tal cual, en la pantalla, a ella. Se
   junta el bloque crudo primero y se resuelve la negrita después. */
function pintarAyuda(texto) {
  const caja = $('#ayuda-texto');
  caja.innerHTML = '';

  let lista = null;     // el <ul>/<ol> abierto
  let punto = null;     // texto CRUDO del punto que se está juntando
  let parrafo = null;   // texto CRUDO del párrafo que se está juntando

  const cerrarPunto = () => {
    if (punto === null) return;
    lista.append(trozosDeLinea(el('li'), punto));
    punto = null;
  };
  const cerrarLista = () => { cerrarPunto(); lista = null; };
  const cerrarParrafo = () => {
    if (parrafo === null) return;
    caja.append(trozosDeLinea(el('p', 'ayuda-parrafo'), parrafo));
    parrafo = null;
  };
  const cerrarTodo = () => { cerrarLista(); cerrarParrafo(); };

  for (const cruda of texto.split(/\r?\n/)) {
    const linea = cruda.trim();

    if (!linea) { cerrarTodo(); continue; }

    if (linea === '---') { cerrarTodo(); caja.append(el('hr', 'ayuda-sep')); continue; }

    // El título del ARCHIVO manda sobre el del panel. Uno solo, y sale del
    // documento: dos títulos que puedan discrepar son dos cosas que mantener.
    if (linea.startsWith('# ')) {
      cerrarTodo();
      $('#ayuda-titulo').textContent = linea.slice(2);
      continue;
    }

    if (linea.startsWith('## ')) {
      cerrarTodo();
      caja.append(trozosDeLinea(el('h3', 'ayuda-seccion'), linea.slice(3)));
      continue;
    }

    // LAS NUMERADAS SON LAS DE «CÓMO PEDIRME AYUDA», y ahí el orden de los
    // pasos ES la instrucción. Sin esto se juntaban los cinco en un párrafo
    // corrido: los números seguían, pero los saltos de línea no.
    const numerada = linea.match(/^\d+\.\s+(.*)$/);
    const vinieta = linea.startsWith('- ');
    if (vinieta || numerada) {
      cerrarParrafo();
      const tipo = numerada ? 'ol' : 'ul';
      if (!lista || lista.tagName.toLowerCase() !== tipo) {
        cerrarLista();
        lista = el(tipo, 'ayuda-lista');
        caja.append(lista);
      } else {
        cerrarPunto();
      }
      punto = numerada ? numerada[1] : linea.slice(2);
      continue;
    }

    // Una línea que sigue a otra CONTINÚA lo anterior. Es lo que hace que un
    // punto partido en dos líneas del archivo se lea como un solo punto.
    if (punto !== null) { punto += ' ' + linea; continue; }
    if (parrafo !== null) { parrafo += ' ' + linea; continue; }
    parrafo = linea;
  }

  cerrarTodo();   // el archivo no termina en línea en blanco garantizada
}

let ayudaPintada = false;
let ayudaFocoPrevio = null;

async function abrirAyuda() {
  if (!ayudaPintada) {
    let texto = '';
    try {
      const r = await pedir('/api/ayuda');
      texto = (r.hay && r.texto) ? r.texto : '';
    } catch (_) { /* se trata igual que si no estuviera */ }

    if (texto.trim()) {
      pintarAyuda(texto);
      ayudaPintada = true;
    } else {
      // QUE FALTE EL DOCUMENTO ES UN PROBLEMA MÍO, NO SUYO. Un panel vacío la
      // dejaría pensando que ha roto algo; lo que necesita en ese momento es
      // el camino que sí existe, y ese camino es escribirme.
      const caja = $('#ayuda-texto');
      caja.innerHTML = '';
      caja.append(el('p', 'ayuda-parrafo',
        'No he podido abrir el documento de ayuda. No es nada que hayas hecho '
        + 'tú y no afecta a tus ofertas ni a tu gasto: escríbeme y te lo mando '
        + 'por correo.'));
    }
  }

  ayudaFocoPrevio = document.activeElement;
  $('#ayuda').hidden = false;
  document.body.style.overflow = 'hidden';
  $('#ayuda .modal-cerrar').focus();
}

function cerrarAyuda() {
  $('#ayuda').hidden = true;
  // Los otros dos paneles pueden estar abiertos por debajo: el scroll solo se
  // devuelve si no queda ninguno.
  if ($('#modal').hidden && $('#confirmar').hidden) {
    document.body.style.overflow = '';
  }
  if (ayudaFocoPrevio) ayudaFocoPrevio.focus();
}

document.querySelectorAll('[data-cerrar-ayuda]').forEach((n) => {
  n.addEventListener('click', cerrarAyuda);
});

$('#pie-dudas').addEventListener('click', abrirAyuda);

/* ---------- el pie: versión y qué hacer si algo va mal ---------- */

/* ESTO ES PARA ARREGLAR COSAS DESDE OTRO CONTINENTE. El desarrollador se
   vuelve a España después de la entrega, así que toda conversación sobre un
   fallo empieza por «¿qué versión tienes?» y sigue por «mándame el log». Las
   dos respuestas tienen que estar en la pantalla, porque ella no va a abrir
   una consola ni a buscar una carpeta.

   El aviso de versión no fiable NO se le enseña a ella: no significa nada
   desde su lado y solo asustaría. Queda en el log, que es quien lo tiene que
   contar. */
let RUTA_DEL_LOG = '';
let CARPETA_DEL_LOG = '';

async function cargarVersion() {
  try {
    const v = await pedir('/api/version');
    RUTA_DEL_LOG = v.log || '';
    CARPETA_DEL_LOG = v.carpeta_log || '';
    $('#pie-version').textContent = `versión ${v.version}`;
    $('#pie-version').title = v.commit ? `commit ${v.commit}` : '';
  } catch (err) {
    // Que no se sepa la versión no puede impedir usar la app. Se dice y ya.
    $('#pie-version').textContent = 'versión desconocida';
  }
}

$('#pie-ayuda').addEventListener('click', async () => {
  const version = $('#pie-version').textContent;
  // DÓNDE MANDARLO, Y NO SOLO A QUIÉN. Este texto decía «mándale a Juan
  // Pablo» y no daba ninguna dirección, igual que la hoja de ayuda: tres
  // sitios distintos pidiéndole que escribiera y ninguno diciendo a dónde
  // (9 septiembre 2026). El dato vive en `AYUDA.md`, en un solo sitio, y
  // desde aquí se apunta a él en vez de repetirlo.
  const texto =
    `Si algo no funciona, mándale a Juan Pablo estos dos datos.
Su correo está al final de «Dudas y ayuda», el botón de al lado.

` +
    `1) La ${version}
` +
    `2) Los archivos de esta carpeta, adjuntos:
` +
    `${CARPETA_DEL_LOG || RUTA_DEL_LOG || '(no disponible)'}

` +
    `Guardan qué hizo la app y qué falló. No contienen ni tu CV ni el texto ` +
    `de las ofertas.`;

  // SE ABRE LA CARPETA, NO SE ENSEÑA LA RUTA. Copiar una ruta deja el
  // siguiente paso —pegarla en el Explorador— a cargo de quien no tiene por
  // qué saber hacerlo, y ese paso es justo el que decide si el log llega o no.
  // Si abrirla falla, el texto copiado sigue teniendo la ruta: se pierde la
  // comodidad, no la información.
  let abierta = false;
  try {
    const r = await pedir('/api/abrir-carpeta-log', { method: 'POST' });
    abierta = !!r.abierta;
  } catch (_) { /* da igual: el texto lleva la ruta */ }

  try {
    await navigator.clipboard.writeText(texto);
    avisar(abierta
      ? 'Te he abierto la carpeta y copiado el texto. Adjunta los archivos.'
      : 'Copiado. Pégalo en un correo.');
  } catch (err) {
    // Sin permiso de portapapeles: se enseña igual, que es lo que importa.
    window.alert(texto);
  }
});

/* =========================================================================
   TUS CRITERIOS

   Lo que se guarda aquí manda sobre `config/cvs.yaml`, y se escribe en
   `data/preferencias.json` — fuera de git. Guardarlo en el YAML dejaría el
   árbol de trabajo sucio y el actualizador se niega a actualizar una copia
   así: la primera vez que ella tocara sus criterios, su portátil dejaría de
   recibir arreglos para siempre. Ver `clasificador/ajustes.py`.
   ========================================================================= */

let ajustesDeFabrica = null;

function pintarCuentaDeCriterios() {
  const lineas = $('#ajustes-criterios').value
    .split('\n').map((l) => l.trim()).filter(Boolean);
  const tope = ajustesDeFabrica?.max || 12;
  $('#ajustes-cuenta').textContent =
    `${lineas.length} de ${tope}. El de arriba manda sobre el de abajo.`;
}

function pintarAjustes(datos) {
  $('#ajustes-criterios').value = (datos.criterios || []).join('\n');
  $('#ajustes-destacar').value = datos.cuantas_destacar ?? 3;
  $('#ajustes-automatica').checked = Boolean(datos.automatica?.activa);
  $('#ajustes-hora').value = datos.automatica?.hora || '07:00';
  $('#ajustes-error').hidden = true;
  pintarCuentaDeCriterios();
}

async function abrirAjustes() {
  let datos;
  try {
    datos = await pedir('/api/ajustes');
  } catch (err) {
    avisar(`No se pudieron leer tus criterios: ${err.message}`);
    return;
  }
  ajustesDeFabrica = { ...datos.de_fabrica, max: datos.max_criterios };
  pintarAjustes(datos);
  $('#ajustes').hidden = false;
  $('#ajustes-criterios').focus();
}

function cerrarAjustes() { $('#ajustes').hidden = true; }

async function guardarAjustes() {
  const criterios = $('#ajustes-criterios').value
    .split('\n').map((l) => l.trim()).filter(Boolean);

  const cuerpo = {
    criterios,
    cuantas_destacar: Number($('#ajustes-destacar').value),
    automatica: {
      activa: $('#ajustes-automatica').checked,
      hora: $('#ajustes-hora').value || '07:00',
    },
  };

  const error = $('#ajustes-error');
  try {
    const guardado = await pedir('/api/ajustes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cuerpo),
    });
    error.hidden = true;
    cerrarAjustes();
    // SE DICE SI EL RELOJ SE PUSO O NO. Encender la casilla y que la tarea no
    // se cree es exactamente el fallo que no puede pasar en silencio.
    if (guardado.automatica?.activa && !guardado.tarea_puesta) {
      avisar('Guardado, pero no se pudo programar la corrida automática. Avísale a Juan Pablo.');
    } else if (guardado.automatica?.activa) {
      avisar(`Guardado. Mañana a las ${guardado.automatica.hora} lo tendrás hecho.`);
    } else {
      avisar('Guardado. El orden cambia la próxima vez que ordenes.');
    }
    await cargarRanking();
  } catch (err) {
    // 422 = lo que escribió no vale, y el motivo está escrito para ella.
    error.textContent = err.message;
    error.hidden = false;
  }
}

$('#pie-ajustes').addEventListener('click', abrirAjustes);
$('#ajustes-guardar').addEventListener('click', guardarAjustes);
$('#ajustes-criterios').addEventListener('input', pintarCuentaDeCriterios);
$('#ajustes-fabrica').addEventListener('click', () => {
  if (!ajustesDeFabrica) return;
  $('#ajustes-criterios').value = (ajustesDeFabrica.criterios || []).join('\n');
  $('#ajustes-destacar').value = ajustesDeFabrica.cuantas_destacar ?? 3;
  pintarCuentaDeCriterios();
});
for (const nodo of document.querySelectorAll('[data-cerrar-ajustes]')) {
  nodo.addEventListener('click', cerrarAjustes);
}

/* ---------- lo que se hizo solo ----------
   Gastar con ella dormida solo es aceptable si al llegar ve qué se hizo y
   cuánto costó. Lo escribe `herramientas/corrida_diaria.py`. */

async function cargarUltimaCorrida() {
  let d;
  try {
    d = await pedir('/api/ultima-corrida');
  } catch (_) { return; }

  const caja = $('#auto-aviso');
  if (!caja) return;
  if (!d.hay) { caja.hidden = true; return; }

  const cuando = new Date(d.cuando);
  const hoy = cuando.toDateString() === new Date().toDateString();
  const cuandoTexto = hoy
    ? `hoy a las ${cuando.toTimeString().slice(0, 5)}`
    : cuando.toLocaleDateString('es-MX', { day: 'numeric', month: 'long' });

  const trozos = [`${d.ofertas_nuevas} oferta(s) nuevas`, `${d.leidas} leída(s)`];
  if (d.coste_usd > 0) trozos.push(`$${d.coste_usd.toFixed(2)}`);

  caja.textContent = `Lo hice yo ${cuandoTexto}: ${trozos.join(' · ')}.`
    + (d.se_paro_por_presupuesto
        ? ' Se paró al llegar a su techo del mes; el resto queda pendiente.' : '');
  caja.hidden = false;
}

/* ---------- arranque ---------- */

/* EL PRIMER ARRANQUE, QUE ES LO ÚNICO QUE NO SE PUEDE REPETIR.

   Antes cualquier fallo de arranque acababa en `avisar()`, un aviso flotante
   que se borra a los 3,8 segundos y deja la pantalla en blanco. Para el caso
   normal —abrir la app recién instalada, sin base de datos todavía— eso
   significaba: cuatro segundos de un texto que le decía que ejecutara un
   comando de Python, y después nada.

   Ahora se queda en pantalla, dice lo que pasa en su idioma, y lleva dentro
   el botón que lo arregla. El 503 no es un error: es «todavía no has empezado».
*/
function pantallaDeArranque(err) {
  const vacio = $('#vacio');
  const boton = $('#vacio-boton');
  const primeraVez = err.status === 503;

  $('#cargando').hidden = true;
  $('#rejilla').innerHTML = '';
  // El orden guardado solo se esconde en el primer arranque, que es cuando no
  // puede existir. Un fallo al cambiar de filtro no es motivo para quitarle de
  // delante lo único que se lee entero.
  if (primeraVez) $('#ranking').hidden = true;

  vacio.querySelector('.vacio-titulo').textContent = primeraVez
    ? 'Todavía no hay ofertas'
    : 'Algo no ha ido bien al abrir';
  vacio.querySelector('.vacio-texto').textContent = primeraVez
    ? err.message
    : `${err.message}\n\nPulsa «Si algo va mal», abajo a la izquierda, y ` +
      'mándale a Juan Pablo lo que te copie.';

  boton.hidden = !primeraVez;
  boton.textContent = 'Buscar ofertas nuevas';
  vacio.hidden = false;
}

$('#vacio-boton').addEventListener('click', () => pedirYConfirmar('recolectar', null));

(async function iniciar() {
  try {
    await cargarVersion();
    await cargarResumen();
    await cargarPresupuesto();
    await cargarRanking();
    await cargarOfertas();

    // Si había una corrida en marcha, se retoma. El trabajo vive en el
    // servidor, no en la pestaña: recargar la página no lo pierde.
    const t = await pedir('/api/trabajos');
    if (t.en_curso) empezarASeguir(t.en_curso);
    cargarUltimaCorrida();
  } catch (err) {
    pantallaDeArranque(err);
  }
})();
