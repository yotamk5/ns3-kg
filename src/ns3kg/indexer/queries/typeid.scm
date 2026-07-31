; ns-3 TypeId extraction candidates. Filtered by name in Python:
; method calls are kept only for AddAttribute/AddTraceSource inside a
; GetTypeId() body, ctor calls only for TypeId("ns3::X").

(call_expression
  function: (field_expression field: (field_identifier) @reg.method)
  arguments: (argument_list) @reg.args) @reg.node

(call_expression
  function: (identifier) @ctor.name
  arguments: (argument_list (string_literal) @ctor.string)) @ctor.node

; TracedCallback<...> member declarations in class bodies.
(field_declaration
  type: (template_type name: (type_identifier) @trace.type)
  declarator: (field_identifier) @trace.name) @trace.node
