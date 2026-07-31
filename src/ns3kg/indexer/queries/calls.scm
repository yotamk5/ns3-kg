; Call-expression candidates. Purely textual: virtual dispatch is not resolved.
(call_expression
  function: [
    (identifier) @call.name
    (field_expression field: (field_identifier) @call.name)
    (qualified_identifier) @call.qname
    (template_function (identifier) @call.name)
  ]) @call.node
