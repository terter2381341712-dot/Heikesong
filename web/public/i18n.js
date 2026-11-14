const preference = 'edge-fabric:ui-language';
export let locale = 'zh-CN';

export function t(chinese, english) {
  return locale === 'en' ? english : chinese;
}

// Keep the original Chinese copy so toggling back needs no page reload.
const original = new WeakMap();
export function setLocale(language, { save = true } = {}) {
  locale = language === 'en' ? 'en' : 'zh-CN';
  document.documentElement.lang = locale;
  for (const element of document.querySelectorAll('[data-en], [data-en-label], [data-en-placeholder], [data-en-content]')) {
    if (!original.has(element)) {
      original.set(element, {
        text: element.textContent,
        'aria-label': element.getAttribute('aria-label'),
        placeholder: element.getAttribute('placeholder'),
        content: element.getAttribute('content'),
      });
    }
    const chinese = original.get(element);
    if (element.hasAttribute('data-en')) element.textContent = t(chinese.text, element.dataset.en);
    for (const [attribute, key] of [['aria-label', 'enLabel'], ['placeholder', 'enPlaceholder'], ['content', 'enContent']]) {
      if (element.dataset[key] !== undefined) element.setAttribute(attribute, t(chinese[attribute], element.dataset[key]));
    }
  }
  document.getElementById('ui-language').value = locale;
  if (save) {
    try { localStorage.setItem(preference, locale); }
    catch { /* Apply the selection even when storage is unavailable. */ }
  }
}

export function restoreLocale() {
  let saved;
  try { saved = localStorage.getItem(preference); }
  catch { /* Use the default locale when storage is unavailable. */ }
  setLocale(saved, { save: false });
}
