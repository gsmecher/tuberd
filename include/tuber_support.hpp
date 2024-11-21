/*
	pybind11/str_enum.h: Declaration and conversion enums as Enum objects.

	Copyright (c) 2020 Ashley Whetter <ashley@awhetter.co.uk>
	Copyright (c) 2022 Graeme Smecher <gsmecher@threespeedlogic.com>

	All rights reserved. Use of this source code is governed by a
	BSD-style license that can be found in the LICENSE.pybind11 file.

	Adapted from https://github.com/pybind/pybind11/pull/2704
*/

#pragma once

#include <pybind11/detail/common.h>
#include <pybind11/pybind11.h>

namespace PYBIND11_NAMESPACE {
	namespace detail {

		template<typename U>
		struct enum_mapper {
			handle type = {};
			std::unordered_map<U, handle> cpp2py = {};
			std::unordered_map<std::string, U> py2cpp = {};

			enum_mapper(handle type, const dict& values) : type(type) {
				for (auto item : values) {
					this->cpp2py[item.second.cast<U>()] = type.attr(item.first);
					this->py2cpp[item.first.cast<std::string>()] = item.second.cast<U>();
				}
			}
		};

		template<typename T>
		struct type_caster<T, enable_if_t<std::is_enum<T>::value>> {
			using underlying_type = typename std::underlying_type<T>::type;

		private:
			using base_caster = type_caster_base<T>;
			using shared_info_type = typename std::unordered_map<std::type_index, void*>;

			static enum_mapper<underlying_type>* enum_info() {
				auto shared_enum_info = reinterpret_cast<shared_info_type*>(get_shared_data("_str_enum_internals"));
				if(shared_enum_info) {
					auto it = shared_enum_info->find(std::type_index(typeid(T)));
					if(it != shared_enum_info->end())
						return reinterpret_cast<enum_mapper<underlying_type>*>(it->second);
				}

				return nullptr;
			}

			base_caster caster;
			T value;

		public:
			template<typename U> using cast_op_type = pybind11::detail::cast_op_type<U>;

			operator T*() { return enum_info() ? &value: static_cast<T*>(caster); }
			operator T&() { return enum_info() ? value: static_cast<T&>(caster); }

			static constexpr auto name = base_caster::name;

			/* C++ -> Python */
			static handle cast(const T& src, return_value_policy policy, handle parent) {
				enum_mapper<underlying_type>* info = enum_info();
				if(info) {
					auto it = info->cpp2py.find(static_cast<underlying_type>(src));
					if(it != info->cpp2py.end())
						return it->second.inc_ref();
				}

				return base_caster::cast(src, policy, parent);
			}

			/* Python -> C++ */
			bool load(handle src, bool convert) {
				if(!src)
					return false;

				enum_mapper<underlying_type>* info = enum_info();
				if(info) {
					std::string val_name;

					/* If the enum value arrives as a string, try to
					 * convert to a C++ value keyed by string value.  If it
					 * arrives as an instantiated Enum type, use that.
					 * Otherwise, refuse to convert. */
					if(pybind11::isinstance<pybind11::str>(src))
						val_name = src.cast<std::string>();
					else if(isinstance(src, info->type))
						val_name = src.attr("value").cast<std::string>();
					else
						return false;

					/* Convert the desired name into a C++ type. */
					auto it = info->py2cpp.find(val_name);
					if(it == info->py2cpp.end())
						return false;

					value = static_cast<T>(it->second);
					return true;
				}

				return caster.load(src, convert);
			}

			static void bind(handle type, const dict& values) {
				enum_mapper<underlying_type>* info = enum_info();
				delete info;

				auto shared_enum_info = &get_or_create_shared_data<shared_info_type>("_str_enum_internals");
				(*shared_enum_info)[std::type_index(typeid(T))] = reinterpret_cast<void*>(
					new enum_mapper<underlying_type>(type, values)
				);
				set_shared_data("_str_enum_internals", shared_enum_info);
			}
		};
	} /* namespace detail */

	template<typename T>
	class str_enum {
	public:
		using underlying_type = typename std::underlying_type<T>::type;

		str_enum(handle scope, const char* name) : scope(scope), name(name) {
			kwargs["value"] = cast(name);
			kwargs["names"] = py_entries;
			kwargs["type"] = module::import("builtins").attr("str");
			if(scope) {
				if(hasattr(scope, "__module__"))
					kwargs["module"] = scope.attr("__module__");

				else if(hasattr(scope, "__name__"))
					kwargs["module"] = scope.attr("__name__");

				if(hasattr(scope, "__qualname__"))
					kwargs["qualname"] = scope.attr("__qualname__").cast<std::string>() + "." + name;
			}
		}

		~str_enum() {
			object ctor = module::import("enum").attr("Enum");
			object unique = module::import("enum").attr("unique");
			object type = unique(ctor(**kwargs));
			setattr(scope, name, type);
			detail::type_caster<T>::bind(type, cpp_entries);

			/* Add a custom __repr__ method that shows a string interpretation */
			pybind11::setattr(type, "__repr__", pybind11::cpp_function(
				[type](pybind11::object self) -> std::string {
					T value = self.cast<T>();

					for (const auto& item : type.attr("__members__").cast<pybind11::dict>())
						if (item.second.cast<T>() == value)
							return '"' + item.first.cast<std::string>() + '"';

					return "UNKNOWN";
				},
				pybind11::is_method(type)
			));
		}

		str_enum& value(const char* name, T value) & {
			add_entry(name, value);
			return *this;
		}

		str_enum&& value(const char* name, T value) && {
			add_entry(name, value);
			return std::move(*this);
		}

		/* Retrieve a name -> name mapping for the Enum. This is attached to a
		 * tuber-exported object as a property. */
		pybind11::dict as_dict() {
			return py_entries;
		}

	private:
		handle scope;
		const char* name;
		dict cpp_entries; /* Maps "name" -> "value" with correct underlying type */
		dict py_entries; /* Maps "name" -> "name"; used for enum.Enum constructor */
		dict kwargs;

		void add_entry(const char* name, T value) {
			cpp_entries[name] = cast(static_cast<underlying_type>(value));
			py_entries[name] = cast(name);
		}
	};

} /* namespace pybind11 */
