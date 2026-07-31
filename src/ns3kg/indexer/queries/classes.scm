; Class and struct definitions (a body is required, so forward
; declarations like "class Foo;" do not match).
(class_specifier
  name: (type_identifier) @def.name
  body: (field_declaration_list)) @def.node

(struct_specifier
  name: (type_identifier) @def.name
  body: (field_declaration_list)) @def.node

(enum_specifier
  name: (type_identifier) @enum.name) @enum.node
