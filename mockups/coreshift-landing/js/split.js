/* ============================================================
   CoreShift — char / word splitter (SplitText replacement)
   Classic script. Depends on nothing. Must run before reveal.js.
   ============================================================ */

window.CS = window.CS || {};

(function () {
  'use strict';

  // Split text into alternating word / whitespace tokens, preserving runs
  // of whitespace exactly as authored.
  function tokenize(text) {
    var out = [];
    var re = /(\s+|\S+)/g;
    var m;
    while ((m = re.exec(text))) {
      out.push({ isSpace: /\s/.test(m[0].charAt(0)), value: m[0] });
    }
    return out;
  }

  function makeWordSpan(word) {
    var span = document.createElement('span');
    span.className = 'reveal-word';
    span.textContent = word;
    return span;
  }

  // Replace a .reveal-word span's plain text with per-character spans.
  function explodeWordChars(wordSpan) {
    var word = wordSpan.textContent;
    wordSpan.textContent = '';
    var chars = [];
    for (var i = 0; i < word.length; i++) {
      var c = document.createElement('span');
      c.className = 'reveal-char';
      c.textContent = word.charAt(i);
      wordSpan.appendChild(c);
      chars.push(c);
    }
    return chars;
  }

  // Rebuild el's contents as word (and optionally char) spans.
  // Handles multiple text-node children and inline <br> elements; any other
  // element children are passed through untouched (not recursed into).
  function splitInto(el, granularity) {
    var frag = document.createDocumentFragment();
    var words = [];
    var chars = [];
    var original = Array.prototype.slice.call(el.childNodes);

    original.forEach(function (node) {
      if (node.nodeType === Node.TEXT_NODE) {
        tokenize(node.textContent).forEach(function (tok) {
          if (tok.isSpace) {
            // Preserve spaces as real text nodes so line-wrapping and
            // spacing behave exactly like normal text.
            frag.appendChild(document.createTextNode(tok.value));
          } else {
            var wordSpan = makeWordSpan(tok.value);
            frag.appendChild(wordSpan);
            words.push(wordSpan);
            if (granularity === 'chars') {
              chars = chars.concat(explodeWordChars(wordSpan));
            }
          }
        });
      } else if (node.nodeType === Node.ELEMENT_NODE && node.tagName === 'BR') {
        frag.appendChild(document.createElement('br'));
      } else {
        frag.appendChild(node);
      }
    });

    el.textContent = ''; // drop any leftover original text nodes
    el.appendChild(frag);
    return { words: words, chars: chars };
  }

  CS.splitWords = function (el) {
    if (!el) return [];
    if (el.hasAttribute('data-cs-split')) {
      return Array.prototype.slice.call(el.querySelectorAll('.reveal-word'));
    }
    var result = splitInto(el, 'words');
    el.setAttribute('data-cs-split', 'words');
    return result.words;
  };

  CS.splitChars = function (el) {
    if (!el) return [];
    if (el.hasAttribute('data-cs-split')) {
      var existingChars = el.querySelectorAll('.reveal-char');
      if (existingChars.length) {
        return Array.prototype.slice.call(existingChars);
      }
      // Was split word-only before; upgrade in place to chars.
      var chars = [];
      Array.prototype.slice.call(el.querySelectorAll('.reveal-word')).forEach(function (w) {
        chars = chars.concat(explodeWordChars(w));
      });
      el.setAttribute('data-cs-split', 'chars');
      return chars;
    }
    var result = splitInto(el, 'chars');
    el.setAttribute('data-cs-split', 'chars');
    return result.chars;
  };
})();
