/**
 * Compatibility shim for the pre-rename widget path.
 *
 * The widget now ships as `botrixai-widget.js`. Pages embedded before that
 * rename request this path with the same query string (token, environment,
 * apiEndpoint), and those are live pages on customer sites -- nobody is going
 * to edit them for us. Forward the request, query string intact.
 */
(function () {
  var self = document.currentScript;
  var src = (self && self.src) || '';
  var query = src.indexOf('?') === -1 ? '' : src.slice(src.indexOf('?'));
  var base = src.slice(0, src.lastIndexOf('/') + 1);

  var js = document.createElement('script');
  js.src = base + 'botrixai-widget.js' + query;
  js.async = true;
  (self && self.parentNode ? self.parentNode : document.head).appendChild(js);
})();
